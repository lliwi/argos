"""Inventario de infraestructura y MCP de Portainer (UC-3)."""

from __future__ import annotations

import httpx

from argos.audit.redact import Redactor
from argos.inventory import Inventory, load_inventory
from argos.mcp_servers.portainer import server as pt
from argos.mcp_servers.portainer.rest import PortainerClient


def _write_inv(root, body):
    (root / "secrets").mkdir(exist_ok=True)
    p = root / "secrets" / "inventory.yaml"
    p.write_text(body)
    p.chmod(0o600)
    return p


def test_inventory_secrets_redacted_and_hidden(tmp_path):
    _write_inv(
        tmp_path,
        """
services:
  portainer:
    url: http://192.168.0.20:9000
    endpoint: 2
    username: admin
    api_key: "ptr_SECRETO_123456"
    notes: "NAS"
""",
    )
    red = Redactor()
    inv = load_inventory(tmp_path, red)
    assert inv.get("portainer")["url"] == "http://192.168.0.20:9000"
    assert inv.secret("portainer", "api_key") == "ptr_SECRETO_123456"
    # el secreto queda registrado en el redactor (no llegará al modelo ni a la auditoría)
    assert "ptr_SECRETO_123456" not in red.redact_text("clave ptr_SECRETO_123456 vista")
    # la vista pública no expone el secreto, solo indica que existe
    view = inv.public_view()["portainer"]
    assert view["url"].endswith(":9000") and view["username"] == "admin"
    assert "api_key" not in view and view["_secretos_configurados"] == ["api_key"]


def test_inventory_missing_file(tmp_path):
    assert load_inventory(tmp_path).services == {}


def test_inventory_warns_on_loose_permissions(tmp_path):
    import logging

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger = logging.getLogger("argos.inventory")
    logger.addHandler(handler)
    try:
        p = _write_inv(tmp_path, "services: {x: {url: http://a}}")
        p.chmod(0o644)
        Inventory.load(p)
    finally:
        logger.removeHandler(handler)
    assert any("permisos" in r.getMessage() for r in records)


async def test_portainer_rest_client():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/containers/json"):
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "abc123def456",
                        "Names": ["/web"],
                        "Image": "nginx",
                        "State": "running",
                        "Status": "Up 2h",
                    }
                ],
            )
        if request.url.path.endswith("/restart"):
            return httpx.Response(204)
        return httpx.Response(200, json={})

    client = PortainerClient(
        "http://portainer:9000", "k", endpoint=2, transport=httpx.MockTransport(handler)
    )
    conts = await client.containers()
    assert conts[0]["Names"] == ["/web"]
    await client.container_action("abc123", "restart")
    await client.aclose()
    assert ("GET", "/api/endpoints/2/docker/containers/json") in calls
    assert ("POST", "/api/endpoints/2/docker/containers/abc123/restart") in calls


def _env(monkeypatch, url="http://portainer:9000", key="k", dry="0"):
    for name, val in {
        "ARGOS_PORTAINER_URL": url,
        "ARGOS_PORTAINER_KEY": key,
        "ARGOS_PORTAINER_ENDPOINT": "1",
        "ARGOS_PORTAINER_DRY_RUN": dry,
    }.items():
        monkeypatch.setenv(name, val)


async def test_portainer_not_configured(monkeypatch):
    monkeypatch.delenv("ARGOS_PORTAINER_URL", raising=False)
    monkeypatch.delenv("ARGOS_PORTAINER_KEY", raising=False)
    assert "NO CONFIGURADO" in await pt.list_containers()
    assert "NO CONFIGURADO" in await pt.stop_container("x")


async def test_portainer_action_dry_run_and_execute(monkeypatch):
    _env(monkeypatch, dry="1")
    assert "[dry-run]" in await pt.restart_container("web")

    _env(monkeypatch, dry="0")
    done = {}

    class FakeClient:
        async def container_action(self, cid, action):
            done["call"] = (cid, action)

        async def aclose(self):
            pass

    monkeypatch.setattr(pt, "_client", lambda: FakeClient())
    assert "restart ejecutado" in await pt.restart_container("web")
    assert done["call"] == ("web", "restart")


async def test_infra_session_inventory_and_portainer_risk(root, store, fake_sandbox):
    """UC-3: infra.inventory expone documentación sin secretos; las acciones de Portainer son
    destructivas (HITL) y respetan dry-run; los secretos del inventario no llegan al modelo."""
    from argos.config import load_config
    from argos.core.session import SessionOptions, run_session
    from argos.governance.approval import ScriptedApprover
    from argos.model.fake import FakeProvider

    _write_inv(
        root,
        """
services:
  portainer:
    url: http://192.168.0.20:9000
    endpoint: 1
    api_key: "ptr_CANARIO_998877"
    notes: "NAS de casa"
""",
    )
    cfg = load_config(root, {"data_dir": str(root / "var")})  # segmento main; infra permitido

    def p(tool, **a):
        return {"type": "tool_call", "tool": tool, "args": a}

    # dry-run explícito: la acción no se ejecuta y no pide aprobación.
    prov = FakeProvider(
        [
            p("infra.inventory"),
            p("portainer.stop_container", container_id="web"),
            {"type": "final", "message": "ok"},
        ]
    )
    res = await run_session(
        SessionOptions(task="mira mi infra", profile="infra", dry_run=True),
        cfg,
        prov,
        store=store,
        sandbox_factory=lambda: fake_sandbox,
    )
    calls = store.events(res.session_id, ["tool_call"])
    inv_call = next(c for c in calls if c.tool == "infra.inventory")
    assert "192.168.0.20" in inv_call.result_preview and "NAS de casa" in inv_call.result_preview
    assert "ptr_CANARIO_998877" not in inv_call.result_preview  # secreto oculto
    stop = next(c for c in calls if c.tool == "portainer.stop_container")
    assert stop.risk_class.value == "destructive" and "dry-run" in stop.result_preview.lower()
    # el secreto del inventario nunca aparece en el prompt del modelo
    assert "ptr_CANARIO_998877" not in prov.requests[-1].render()

    # sin dry-run: parar un contenedor exige aprobación (RF-GOV-04); denegada => no se ejecuta.
    prov2 = FakeProvider(
        [p("portainer.stop_container", container_id="web"), {"type": "final", "message": "ok"}]
    )
    res2 = await run_session(
        SessionOptions(task="para web", profile="infra", dry_run=False),
        cfg,
        prov2,
        store=store,
        approver=ScriptedApprover(["denied"]),
        sandbox_factory=lambda: fake_sandbox,
    )
    appr = store.events(res2.session_id, ["approval"])
    assert appr and appr[0].risk_class.value == "destructive" and appr[0].decision == "denied"


async def test_homeassistant_rest_client():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "light.salon",
                        "state": "off",
                        "attributes": {"friendly_name": "Salón"},
                    },
                    {"entity_id": "sensor.temp", "state": "21.5", "attributes": {}},
                ],
            )
        return httpx.Response(200, json={"ok": True})

    from argos.mcp_servers.homeassistant.rest import HomeAssistantClient

    client = HomeAssistantClient("http://ha:8123", "tok", transport=httpx.MockTransport(handler))
    states = await client.states()
    assert states[0]["entity_id"] == "light.salon"
    await client.call_service("light", "turn_on", "light.salon")
    await client.aclose()
    assert ("POST", "/api/services/light/turn_on") in calls


def _ha_env(monkeypatch, url="http://ha:8123", token="tok", dry="0"):
    for name, val in {
        "ARGOS_HA_URL": url,
        "ARGOS_HA_TOKEN": token,
        "ARGOS_HA_DRY_RUN": dry,
    }.items():
        monkeypatch.setenv(name, val)


async def test_homeassistant_not_configured(monkeypatch):
    monkeypatch.delenv("ARGOS_HA_URL", raising=False)
    monkeypatch.delenv("ARGOS_HA_TOKEN", raising=False)
    from argos.mcp_servers.homeassistant import server as ha

    assert "NO CONFIGURADO" in await ha.list_entities()
    assert "NO CONFIGURADO" in await ha.call_service("light", "turn_on", "light.x")


async def test_homeassistant_action_validation_dry_run_execute(monkeypatch):
    from argos.mcp_servers.homeassistant import server as ha

    _ha_env(monkeypatch, dry="1")
    assert "[dry-run]" in await ha.call_service("light", "turn_on", "light.salon")
    # formato inválido se rechaza antes de tocar la red
    assert "RECHAZADO" in await ha.call_service("light", "turn on", "light.salon")
    assert "RECHAZADO" in await ha.call_service("light", "turn_on", "malo")

    _ha_env(monkeypatch, dry="0")
    done = {}

    class FakeClient:
        async def call_service(self, d, s, e):
            done["call"] = (d, s, e)

        async def aclose(self):
            pass

    monkeypatch.setattr(ha, "_client", lambda: FakeClient())
    assert "light.turn_on ejecutado" in await ha.call_service("light", "turn_on", "light.salon")
    assert done["call"] == ("light", "turn_on", "light.salon")


async def test_homeassistant_in_infra_catalog(root, store, fake_sandbox):
    from argos.core.session import catalog

    items = await catalog(load_cfg(root), "infra")
    names = {t["name"] for t in items}
    assert "homeassistant.call_service" in names and "homeassistant.list_entities" in names
    action = next(t for t in items if t["name"] == "homeassistant.call_service")
    assert action["risk"] == "destructive"


def load_cfg(root):
    from argos.config import load_config

    return load_config(root, {"data_dir": str(root / "var")})
