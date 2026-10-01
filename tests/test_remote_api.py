"""API del núcleo por TCP para producción (IP macvlan, ADR-0025): TLS + token, fail-closed."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from argos.scheduler import SchedulerCfg
from argos.server.app import BearerAuth, Core, TcpApi, serve
from argos.server.client import CoreClient, CoreUnavailable

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40


def _cert(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "api-cert.pem", tmp / "api-key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
         "-nodes", "-days", "1", "-subj", "/CN=argos", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_tcp_api_is_fail_closed(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    with pytest.raises(ValueError, match="ARGOS_API_TOKEN"):
        TcpApi("0.0.0.0", 8788, "", cert, key).check()
    with pytest.raises(ValueError, match="ARGOS_API_TOKEN"):
        TcpApi("0.0.0.0", 8788, "corto", cert, key).check()
    with pytest.raises(ValueError, match="TLS obligatorio"):
        TcpApi("0.0.0.0", 8788, TOKEN, cert, key).check()


async def test_bearer_auth_rejects_missing_or_wrong_token():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(_req):
        return JSONResponse({"ok": True})

    app = BearerAuth(Starlette(routes=[Route("/health", ok)]), TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://x") as c:
        assert (await c.get("/health")).status_code == 401
        bad = {"Authorization": "Bearer " + "x" * 40}
        assert (await c.get("/health", headers=bad)).status_code == 401
        r = await c.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.json() == {"ok": True}


def test_client_refuses_plain_http_and_missing_token(tmp_path):
    with pytest.raises(CoreUnavailable, match="https"):
        CoreClient(url="http://192.168.0.34:8788", token=TOKEN)
    with pytest.raises(CoreUnavailable, match="ARGOS_API_TOKEN"):
        CoreClient(url="https://192.168.0.34:8788", token="")
    with pytest.raises(CoreUnavailable, match="certificado"):
        CoreClient(url="https://192.168.0.34:8788", token=TOKEN, ca=tmp_path / "no.pem")


def test_connect_uses_socket_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ARGOS_API_URL", raising=False)
    with pytest.raises(CoreUnavailable, match="no está en marcha"):
        CoreClient.connect(tmp_path / "no.sock", tmp_path)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="requiere openssl")
async def test_remote_client_over_tls_with_token(cfg, store, monkeypatch, tmp_path):
    from argos.model.fake import FakeProvider

    cert, key = _cert(tmp_path)
    port = _free_port()
    core = Core(cfg, store, lambda: FakeProvider([{"type": "final", "message": "ok"}]),
                SchedulerCfg())  # fmt: skip
    sock = cfg.data_path / "run" / "t.sock"
    tcp = TcpApi("127.0.0.1", port, TOKEN, cert, key)
    task = asyncio.create_task(serve(core, sock, None, None, run_scheduler=False, tcp_api=tcp))
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            await asyncio.sleep(0.05)
        # Como lo configura secrets/api-client.env (CA relativo a la raíz del repo).
        monkeypatch.setenv("ARGOS_API_URL", f"https://127.0.0.1:{port}")
        monkeypatch.setenv("ARGOS_API_TOKEN", TOKEN)
        monkeypatch.setenv("ARGOS_API_CA", cert.name)
        async with CoreClient.connect(tmp_path / "no-local.sock", tmp_path) as client:
            assert (await client.health())["segment"] == "main"
        # Sin token: 401. Sin fijar el certificado autofirmado: falla la verificación TLS.
        async with httpx.AsyncClient(verify=ssl.create_default_context(cafile=str(cert))) as raw:
            assert (await raw.get(f"https://127.0.0.1:{port}/health")).status_code == 401
        async with httpx.AsyncClient() as raw:
            with pytest.raises(httpx.ConnectError):
                await raw.get(f"https://127.0.0.1:{port}/health")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_prod_override_only_puts_daemon_on_lan():
    prod = yaml.safe_load((ROOT / "compose.prod.yaml").read_text())
    assert set(prod["services"]) == {"daemon"}
    daemon = prod["services"]["daemon"]
    assert daemon["networks"]["int-lan"]["ipv4_address"] == "${ARGOS_LAN_IP:-192.168.0.34}"
    assert "egress_main" in daemon["networks"]
    assert {"path": "secrets/api.env", "required": True} in daemon["env_file"]
    assert "--api-port" in daemon["command"]
    assert prod["networks"]["int-lan"]["external"] is True


@pytest.mark.skipif(shutil.which("openssl") is None, reason="requiere openssl")
def test_api_setup_writes_private_files(root, monkeypatch):
    from typer.testing import CliRunner

    import argos.cli as cli
    from argos.config import load_config

    (root / "secrets").mkdir()
    monkeypatch.setattr(cli, "load_config", lambda: load_config(root))
    res = CliRunner().invoke(cli.app, ["api-setup", "192.168.0.34"])
    assert res.exit_code == 0, res.output
    sec = root / "secrets"
    for name in ("api.env", "api-client.env", "api-cert.pem", "api-key.pem"):
        assert (sec / name).stat().st_mode & 0o777 == 0o600, name
    server = (sec / "api.env").read_text()
    client = dict(line.split("=", 1) for line in (sec / "api-client.env").read_text().splitlines())
    assert client["ARGOS_API_URL"] == "https://192.168.0.34:8788"
    assert server == f"ARGOS_API_TOKEN={client['ARGOS_API_TOKEN']}\n"
    assert len(client["ARGOS_API_TOKEN"]) >= 32 and client["ARGOS_API_TOKEN"] not in res.output
    san = subprocess.run(
        ["openssl", "x509", "-in", str(sec / "api-cert.pem"), "-noout", "-ext", "subjectAltName"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert "IP Address:192.168.0.34" in san
    # No pisa una configuración existente sin --force; no acepta nombres en vez de IP.
    assert CliRunner().invoke(cli.app, ["api-setup", "192.168.0.34"]).exit_code == 1
    assert CliRunner().invoke(cli.app, ["api-setup", "argos.lan", "--force"]).exit_code == 2
