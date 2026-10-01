"""Servidor MCP de Home Assistant para gestión del hogar propio (UC-3).

- Lectura (listar entidades, ver estado): riesgo `read`, sin aprobación.
- Acciones (llamar a un servicio: encender/apagar, etc.): riesgo `destructive` (meta), obliga a
  aprobación humana en Argos (RF-GOV-04) y respeta dry-run (RF-19).
- URL y token llegan por entorno desde el inventario, nunca del modelo. Sin plantillas ni comandos.

Entorno (lo inyecta Argos): ARGOS_HA_URL, ARGOS_HA_TOKEN, ARGOS_HA_DRY_RUN ("1"/"0").
"""

from __future__ import annotations

import json
import os
import re

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.homeassistant.rest import HomeAssistantClient, HomeAssistantError

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}

# Identificadores simples: dominio/servicio HA (letras, dígitos, guion bajo) y entity_id (dom.obj).
_IDENT = re.compile(r"^[a-z0-9_]+$")
_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

server = MCPServer(name="homeassistant", version=VERSION)


def _configured() -> str | None:
    if not os.environ.get("ARGOS_HA_URL") or not os.environ.get("ARGOS_HA_TOKEN"):
        return (
            "Home Assistant no está configurado: falta url o token en secrets/inventory.yaml "
            "(servicio 'homeassistant')."
        )
    return None


def _client() -> HomeAssistantClient:
    return HomeAssistantClient(
        os.environ["ARGOS_HA_URL"],
        os.environ["ARGOS_HA_TOKEN"],
        verify=os.environ.get("ARGOS_HA_VERIFY", "1") != "0",
    )


def _dry_run() -> bool:
    return os.environ.get("ARGOS_HA_DRY_RUN", "0") != "0"


@server.tool(annotations=READ)
async def list_entities(domain: str = "") -> str:
    """Lista entidades (id, estado, nombre). Filtra por dominio si se indica (light, switch,
    sensor, climate…)."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if domain and not _IDENT.match(domain):
        return "dominio inválido"
    client = _client()
    try:
        data = await client.states()
        brief = [
            {
                "entity_id": e.get("entity_id"),
                "state": e.get("state"),
                "name": (e.get("attributes") or {}).get("friendly_name"),
            }
            for e in data
            if not domain or str(e.get("entity_id", "")).startswith(f"{domain}.")
        ]
        return json.dumps(brief, ensure_ascii=False, indent=2)[:12000]
    except HomeAssistantError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def get_state(entity_id: str) -> str:
    """Devuelve el estado y atributos de una entidad (p. ej. light.salon)."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if not _ENTITY.match(entity_id):
        return "entity_id inválido (formato dominio.objeto)"
    client = _client()
    try:
        return json.dumps(await client.state(entity_id), ensure_ascii=False, indent=2)[:8000]
    except HomeAssistantError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def call_service(domain: str, service: str, entity_id: str) -> str:
    """Llama a un servicio sobre una entidad (p. ej. domain=light, service=turn_on,
    entity_id=light.salon). Requiere aprobación."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if not (_IDENT.match(domain) and _IDENT.match(service) and _ENTITY.match(entity_id)):
        return "RECHAZADO: domain/service/entity_id con formato inválido"
    if _dry_run():
        return f"[dry-run] no se ejecuta. Acción prevista: {domain}.{service} sobre {entity_id}."
    client = _client()
    try:
        await client.call_service(domain, service, entity_id)
        return f"{domain}.{service} ejecutado sobre {entity_id}."
    except HomeAssistantError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
