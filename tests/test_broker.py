"""Broker de sandbox (ADR-0008): validación, aislamiento por segmento y ejecución real."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from conftest import FakeSandbox
from test_egress_eval import _docker_ready

from argos.sandbox import broker as broker_mod
from argos.sandbox import broker_policy as policy
from argos.sandbox.broker_client import BrokerSandbox
from argos.sandbox.docker_sandbox import EgressDecision, ExecResult, SandboxError


def test_policy_validation(tmp_path):
    with pytest.raises(policy.PolicyError):
        policy.session_id("../../etc")
    with pytest.raises(policy.PolicyError):
        policy.domains(["evil.com; rm -rf /"])
    with pytest.raises(policy.PolicyError, match="reservada"):
        policy.env({"HTTPS_PROXY": "http://yo"})
    with pytest.raises(policy.PolicyError):
        policy.timeout(10_000)
    with pytest.raises(policy.PolicyError, match="workspace"):
        policy.workspace(tmp_path, "a" * 32)
    assert policy.domains(["PyPI.org", "*.github.com"]) == ["pypi.org", "*.github.com"]


class FakeDocker(FakeSandbox):
    """Sustituye a DockerSandbox dentro del broker: registra con qué parámetros se crea."""

    created: list[dict] = []

    def __init__(self, **kw):
        super().__init__()
        FakeDocker.created.append(kw)
        self.id = f"fake-{kw['session_id'][:6]}"
        self.env = kw["env"]
        self.blocked = [EgressDecision("evil.test", 443, "blocked", "no en allowlist")]

    async def start(self):
        pass


@contextlib.asynccontextmanager
async def running_broker(cfg, monkeypatch):
    FakeDocker.created = []
    monkeypatch.setattr(broker_mod, "DockerSandbox", FakeDocker)
    b = broker_mod.Broker(cfg)
    task = asyncio.create_task(b.serve())
    for _ in range(100):
        if all(cfg.broker_socket(s).exists() for s in cfg.segments):
            break
        await asyncio.sleep(0.02)
    try:
        yield b
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _workspace(cfg, segment="main"):
    sid = uuid.uuid4().hex
    ws = cfg.base_path / "segments" / segment / "workspaces" / sid
    (ws / "in").mkdir(parents=True)
    (ws / "out").mkdir()
    return sid, ws


async def test_broker_creates_with_its_own_config(cfg, monkeypatch):
    sid, ws = _workspace(cfg)
    async with running_broker(cfg, monkeypatch) as b:
        box = BrokerSandbox(cfg.broker_socket("main"), sid, ["pypi.org"], {"TOKEN": "x"})
        res = await box.exec("echo hola")
        assert isinstance(res, ExecResult) and res.exit_code == 0
        assert [d.host for d in box.egress_blocked_since_last()] == ["evil.test"]
        created = FakeDocker.created[0]
        # Red, proxy y workspace los decide el broker, no el cliente.
        assert created["network"] == "argos_sandbox_main"
        assert created["proxy_url"] == "http://egress-main:3128"
        assert created["workspace"] == ws and created["segment"] == "main"
        await box.destroy()
        assert b.boxes == {}
        ops = [line for line in b.log_path.read_text().splitlines()]
        assert len(ops) == 3 and "TOKEN" in ops[0] and '"x"' not in ops[0]  # nombres, no valores


async def test_segment_isolation(cfg, monkeypatch):
    sid, _ = _workspace(cfg, "main")
    async with running_broker(cfg, monkeypatch):
        await BrokerSandbox(cfg.broker_socket("main"), sid).exec("true")
        # Desde el socket de osint, esa sesión no existe…
        with pytest.raises(SandboxError, match="no hay sandbox"):
            await BrokerSandbox(cfg.broker_socket("osint"), sid)._call(
                "exec", command="cat /etc/passwd")
        # …ni puede crearse: su workspace no está en el segmento osint.
        with pytest.raises(SandboxError, match="workspace"):
            await BrokerSandbox(cfg.broker_socket("osint"), sid).exec("true")


async def test_invalid_requests_are_rejected(cfg, monkeypatch):
    sid, _ = _workspace(cfg)
    async with running_broker(cfg, monkeypatch):
        with pytest.raises(SandboxError, match="reservada"):
            await BrokerSandbox(cfg.broker_socket("main"), sid, env={"PATH": "/tmp"}).exec("id")
        with pytest.raises(SandboxError, match="session_id"):
            await BrokerSandbox(cfg.broker_socket("main"), "nope").exec("id")
    assert FakeDocker.created == []


@pytest.mark.docker
async def test_real_broker_end_to_end(cfg):
    if not _docker_ready(cfg.sandbox.image):
        pytest.skip("imagen de sandbox o red no disponibles")
    sid, _ = _workspace(cfg)
    b = broker_mod.Broker(cfg)
    task = asyncio.create_task(b.serve())
    for _ in range(100):
        if cfg.broker_socket("main").exists():
            break
        await asyncio.sleep(0.02)
    box = BrokerSandbox(cfg.broker_socket("main"), sid)
    try:
        res = await box.exec("id -u; echo ok > /workspace/out/f; cat /workspace/out/f")
        assert res.stdout.split() == ["1000", "ok"]
    finally:
        await box.destroy()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
