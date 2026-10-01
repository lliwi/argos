"""MCP del backend OSINT propio (UC-1, ADR-0023)."""

from __future__ import annotations

import json

import httpx

from argos.mcp_servers.osint.rest import OsintClient

TID = "9bfee208-7bd8-44f3-8ed2-6b3a30d59eb4"
DONE = {
    "task_id": TID,
    "workflow": "domain_recon",
    "target": "ejemplo.com",
    "status": "completed",
    "error": None,
    "result": {
        "workflow": "domain_recon",
        "target": "ejemplo.com",
        "summary": "Domain recon for 'ejemplo.com': 2 findings from 3 sources.",
        "confidence": "high",
        "risk": "low",
        "findings": [
            {
                "type": "dns_a",
                "value": ["203.0.113.7"],
                "source": "dig",
                "confidence": "high",
                "notes": "",
            },
            {"type": "org", "value": "Ejemplo SL", "source": "Shodan", "confidence": "high"},
        ],
        "entities": [],
        "relationships": [],
        "sources": [{"name": "dig_A", "success": True}, {"name": "whois", "success": False}],
        "raw_output_path": "/srv/osint/raw/secret-path.json",
        "warnings": [],
    },
}


def _env(monkeypatch):
    monkeypatch.setenv("ARGOS_OSINT_URL", "https://osint.test")
    monkeypatch.setenv("ARGOS_OSINT_KEY", "k3y")


def _fake(monkeypatch, handler):
    from argos.mcp_servers.osint import server as osint

    calls: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return handler(req)

    monkeypatch.setattr(
        osint,
        "_client",
        lambda: OsintClient("https://osint.test", "k3y", transport=httpx.MockTransport(wrapped)),
    )
    return calls


def _routes(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/workflow/run":
        return httpx.Response(200, json={"task_id": TID, "status": "running"})
    if req.url.path == f"/tasks/{TID}":
        return httpx.Response(200, json=DONE)
    if req.url.path == "/tools":
        return httpx.Response(
            200,
            json=[
                {
                    "name": "whois",
                    "category": "domain",
                    "inputs": ["domain"],
                    "requires_api_key": False,
                    "risk_level": "low",
                    "enabled": True,
                    "description": "WHOIS lookup",
                    "binary": "whois",
                }
            ],
        )
    if req.url.path == f"/reports/{TID}":
        return httpx.Response(200, text="# OSINT Report — ejemplo.com")
    return httpx.Response(404, json={"detail": "Not Found"})


async def test_osint_not_configured(monkeypatch):
    monkeypatch.delenv("ARGOS_OSINT_URL", raising=False)
    monkeypatch.delenv("ARGOS_OSINT_KEY", raising=False)
    from argos.mcp_servers.osint import server as osint

    assert "NO CONFIGURADO" in await osint.catalog()
    assert "NO CONFIGURADO" in await osint.recon("domain_recon", "ejemplo.com")


def test_normalize_target():
    from argos.mcp_servers.osint.server import normalize_target as n

    assert n("domain", "Ejemplo.COM.") == "ejemplo.com"
    assert n("ip", " 8.8.8.8 ") == "8.8.8.8"
    assert n("email", "Ana@Ejemplo.com") == "ana@ejemplo.com"
    assert n("email_or_domain", "ejemplo.com") == "ejemplo.com"
    assert n("username", "@lliwi") == "lliwi"
    assert n("phone", "+34 600 11-22-33") == "+34600112233"
    assert n("plate", "1234 abc") == "1234ABC"
    assert n("text", "Ejemplo SL") == "Ejemplo SL"
    for kind, bad in [
        ("domain", "ejemplo"),
        ("domain", "a b.com"),
        ("ip", "999.1.1.1"),
        ("email", "no-email"),
        ("username", "a b"),
        ("phone", "llámame"),
        ("plate", "../x"),
        ("text", "x"),
        ("text", "línea\nnueva"),
    ]:
        assert n(kind, bad) is None, (kind, bad)


async def test_recon_runs_workflow_and_shapes_result(monkeypatch):
    from argos.mcp_servers.osint import server as osint

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    out = await osint.recon("domain_recon", "Ejemplo.com")
    assert out.startswith(osint.UNTRUSTED)
    data = json.loads(out[len(osint.UNTRUSTED) :])
    assert data["summary"].startswith("Domain recon") and data["task_id"] == TID
    assert data["sources_ok"] == ["dig_A"] and data["sources_failed"] == ["whois"]
    assert data["findings"][0] == {
        "type": "dns_a",
        "value": ["203.0.113.7"],
        "source": "dig",
        "confidence": "high",
    }
    assert "secret-path" not in out  # rutas internas del servidor: fuera
    body = json.loads(calls[0].content)
    assert body == {
        "workflow": "domain_recon",
        "target": "ejemplo.com",
        "mode": "safe",
        "location": "",
    }
    assert all(c.headers["x-osint-api-key"] == "k3y" for c in calls)


async def test_validation_rejects_before_network(monkeypatch):
    from argos.mcp_servers.osint import server as osint

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    assert "usa osint.person" in await osint.recon("person_recon", "Ana García")
    assert "RECHAZADO" in await osint.recon("domain_recon", "no es un dominio")
    assert "usa osint.recon" in await osint.person("domain_recon", "ejemplo.com", "x" * 20)
    assert "purpose obligatorio" in await osint.person("email_reputation", "a@b.com", "")
    assert "purpose obligatorio" in await osint.person("email_reputation", "a@b.com", "porque")
    assert "RECHAZADO" in await osint.person("phone_reputation", "abc", "verificar mi número")
    assert "RECHAZADO" in await osint.person(
        "person_recon", "Ana García", "verificar identidad de proveedor", email="mal"
    )
    assert "task_id inválido" in await osint.result("../../tools/import")
    assert calls == []


async def test_person_sends_purposeful_request(monkeypatch):
    from argos.mcp_servers.osint import server as osint

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    out = await osint.person(
        "person_recon",
        "Ana García",
        "due diligence de un proveedor antes de contratar",
        company="Ejemplo SL",
        phone="+34 600 112 233",
    )
    assert out.startswith(osint.UNTRUSTED)
    body = json.loads(calls[0].content)
    assert body == {
        "workflow": "person_recon",
        "target": "Ana García",
        "mode": "safe",
        "location": "",
        "company": "Ejemplo SL",
        "phone": "+34600112233",
    }
    assert "purpose" not in body  # la finalidad queda en la auditoría de Argos, no se envía


def test_shape_running_and_failed():
    from argos.mcp_servers.osint.server import shape

    assert "osint.result" in shape({"task_id": TID, "status": "running"})
    out = shape({"task_id": TID, "status": "failed", "error": "Unknown workflow"})
    assert out.startswith("ERROR (failed)") and "Unknown workflow" in out


async def test_catalog_report_and_bad_key(monkeypatch):
    from argos.mcp_servers.osint import server as osint

    _env(monkeypatch)
    _fake(monkeypatch, _routes)
    cat = json.loads(await osint.catalog())
    assert cat[0]["name"] == "whois" and "binary" not in cat[0]
    assert "# OSINT Report" in await osint.report(TID)
    _fake(monkeypatch, lambda req: httpx.Response(401, json={"detail": "Not authenticated"}))
    assert "api_key rechazada" in await osint.recon("domain_recon", "ejemplo.com")


async def test_osint_only_in_osint_catalog(root, store, fake_sandbox, monkeypatch):
    from argos.config import load_config
    from argos.core.session import catalog

    _env(monkeypatch)
    cfg = load_config(root, {"data_dir": str(root / "var")})
    by = {t["name"]: t["risk"] for t in await catalog(cfg, "osint")}
    for read in ("catalog", "recon", "result", "report"):
        assert by.get(f"osint.{read}") == "read", read
    assert by.get("osint.person") == "offensive"  # => aprobación humana (RF-GOV-04)
    assert "offensive" in cfg.approval.require_for
    for other in ("infra", "orchestrator", "personal"):
        names = {t["name"] for t in await catalog(cfg, other)}
        assert not any(n.startswith("osint.") for n in names), other


def test_credentials_fall_back_to_env_and_are_redacted(monkeypatch):
    from argos.audit.redact import Redactor
    from argos.core.session import osint_credentials
    from argos.inventory import Inventory

    monkeypatch.setenv("ARGOS_OSINT_URL", "https://osint.test")
    monkeypatch.setenv("ARGOS_OSINT_KEY", "clave-larga-123")
    red = Redactor()
    assert osint_credentials(Inventory({}), red) == ("https://osint.test", "clave-larga-123")
    assert "clave-larga-123" not in red.redact_secrets("la clave es clave-larga-123")
    inv = Inventory({"osint-mcp": {"url": "https://inv.test", "api_key": "otra"}})
    assert osint_credentials(inv) == ("https://inv.test", "otra")
