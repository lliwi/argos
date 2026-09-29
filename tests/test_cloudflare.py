"""MCP de Cloudflare: zonas, DNS, túneles y analítica (UC-3)."""

from __future__ import annotations

import json

import httpx

from argos.mcp_servers.cloudflare.rest import CloudflareClient

ZONE = {"id": "z" * 32, "name": "ejemplo.com", "status": "active", "plan": {"name": "Free"},
        "account": {"id": "a" * 32}}
REC = {"id": "1" * 32, "type": "CNAME", "name": "app.ejemplo.com",
       "content": "abc.cfargotunnel.com", "proxied": True, "ttl": 1}


def _env(monkeypatch, dry="0"):
    monkeypatch.setenv("ARGOS_CF_TOKEN", "tok")
    monkeypatch.setenv("ARGOS_CF_DRY_RUN", dry)


def _fake(monkeypatch, handler):
    from argos.mcp_servers.cloudflare import server as cf

    calls: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return handler(req)

    monkeypatch.setattr(cf, "_client", lambda: CloudflareClient(
        "tok", transport=httpx.MockTransport(wrapped)))
    return calls


def _ok(result, pages=1):
    return httpx.Response(200, json={"success": True, "errors": [], "result": result,
                                     "result_info": {"total_pages": pages}})


def _routes(req: httpx.Request) -> httpx.Response:
    path = req.url.path
    if path.endswith("/zones"):
        return _ok([ZONE])
    if path.endswith("/dns_records") and req.method == "GET":
        return _ok([REC])
    if path.endswith("/dns_records") and req.method == "POST":
        return _ok({**json.loads(req.content), "id": "2" * 32})
    if path.endswith(f"/dns_records/{REC['id']}") and req.method == "PATCH":
        return _ok({**REC, **json.loads(req.content)})
    if path.endswith(f"/dns_records/{REC['id']}") and req.method == "DELETE":
        return _ok({"id": REC["id"]})
    if path.endswith("/cfd_tunnel"):
        return httpx.Response(403, json={"success": False, "result": None, "errors": [
            {"code": 9109, "message": "Unauthorized to access requested resource"}]})
    return httpx.Response(404, json={"success": False, "errors": [{"code": 7000}]})


async def test_cloudflare_not_configured(monkeypatch):
    monkeypatch.delenv("ARGOS_CF_TOKEN", raising=False)
    from argos.mcp_servers.cloudflare import server as cf

    assert "NO CONFIGURADO" in await cf.zones()
    assert "NO CONFIGURADO" in await cf.dns_delete("ejemplo.com", REC["id"])


async def test_fqdn_stays_inside_zone():
    from argos.mcp_servers.cloudflare.server import _fqdn

    assert _fqdn("@", "ejemplo.com") == "ejemplo.com"
    assert _fqdn("www", "ejemplo.com") == "www.ejemplo.com"
    assert _fqdn("App.Ejemplo.com.", "ejemplo.com") == "app.ejemplo.com"
    assert _fqdn("*", "ejemplo.com") == "*.ejemplo.com"
    assert _fqdn("a.b.ejemplo.com", "ejemplo.com") == "a.b.ejemplo.com"
    for bad in ("otro.org", "x.ejemplo.com.otro.org", "a b", "x/../y", "-x", "..ejemplo.com"):
        assert _fqdn(bad, "ejemplo.com") is None, bad


async def test_dns_list_and_create(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    out = await cf.dns_list("ejemplo.com")
    assert "cfargotunnel.com" in out and REC["id"] in out
    out = await cf.dns_create("ejemplo.com", "A", "nas", "203.0.113.7", proxied=True)
    assert "Registro creado" in out
    body = json.loads(next(c for c in calls if c.method == "POST").content)
    assert body == {"type": "A", "name": "nas.ejemplo.com", "content": "203.0.113.7", "ttl": 1,
                    "proxied": True}
    assert all(c.headers["authorization"] == "Bearer tok" for c in calls)


async def test_dns_validation_rejects_before_network(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    assert "RECHAZADO" in await cf.dns_create("ejemplo.com", "A", "x", "no-es-ip")
    assert "RECHAZADO" in await cf.dns_create("ejemplo.com", "SRV", "x", "a")
    assert "RECHAZADO" in await cf.dns_create("ejemplo.com", "TXT", "x", "a\nb")
    assert "RECHAZADO" in await cf.dns_delete("ejemplo.com", "../../zones")
    assert "RECHAZADO" in await cf.dns_update("ejemplo.com", REC["id"], proxied="quizá")
    assert calls == []
    assert "RECHAZADO" in await cf.dns_create("ejemplo.com", "A", "x.otro.org", "1.2.3.4")


async def test_dns_actions_dry_run(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch, dry="1")
    calls = _fake(monkeypatch, _routes)
    assert "[dry-run]" in await cf.dns_create("ejemplo.com", "CNAME", "www", "ejemplo.com")
    assert "[dry-run]" in await cf.dns_update("ejemplo.com", REC["id"], proxied="false")
    assert "[dry-run]" in await cf.dns_delete("ejemplo.com", REC["id"])
    assert all(c.method == "GET" for c in calls)


async def test_dns_update_and_delete(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch)
    calls = _fake(monkeypatch, _routes)
    assert "Registro actualizado" in await cf.dns_update("ejemplo.com", REC["id"],
                                                         proxied="false")
    assert json.loads(next(c for c in calls if c.method == "PATCH").content) == {"proxied": False}
    assert "Registro borrado: CNAME app.ejemplo.com" in await cf.dns_delete("ejemplo.com",
                                                                            REC["id"])


async def test_missing_permission_is_explained(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch)
    _fake(monkeypatch, _routes)
    out = await cf.tunnels()
    assert out.startswith("SIN PERMISO") and "Cloudflare Tunnel" in out


async def test_cloudflare_in_infra_catalog(root, store, fake_sandbox):
    from argos.config import load_config
    from argos.core.session import catalog

    items = await catalog(load_config(root, {"data_dir": str(root / "var")}), "infra")
    by = {t["name"]: t["risk"] for t in items}
    assert by.get("cloudflare.zones") == "read" and by.get("cloudflare.dns_list") == "read"
    assert by.get("cloudflare.tunnels") == "read" and by.get("cloudflare.analytics") == "read"
    for action in ("dns_create", "dns_update", "dns_delete"):
        assert by.get(f"cloudflare.{action}") == "destructive"


async def test_empty_tunnel_list_hints_permission(monkeypatch):
    from argos.mcp_servers.cloudflare import server as cf

    _env(monkeypatch)
    _fake(monkeypatch, lambda req: _ok([ZONE] if req.url.path.endswith("/zones") else []))
    assert "Cloudflare Tunnel › Read" in await cf.tunnels()
