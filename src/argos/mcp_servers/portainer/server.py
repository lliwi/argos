"""Servidor MCP de Portainer para gestión de infraestructura propia (UC-3).

- Lectura (listar/inspeccionar/logs): riesgo `read`, sin aprobación.
- Acciones de ciclo de vida (start/stop/restart de contenedores): riesgo `destructive` (meta), lo
  que obliga a aprobación humana en Argos (RF-GOV-04) y respeta dry-run (RF-19).
- La URL y la API key llegan por entorno desde el inventario (nunca del modelo). Sin ejecución de
  comandos arbitrarios.

Configuración por entorno (la inyecta Argos): ARGOS_PORTAINER_URL, ARGOS_PORTAINER_KEY,
ARGOS_PORTAINER_ENDPOINT, ARGOS_PORTAINER_DRY_RUN ("1"/"0").
"""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from argos.mcp_servers.portainer.rest import PortainerClient, PortainerError

VERSION = "0.1.0"
READ = ToolAnnotations(read_only_hint=True, idempotent_hint=True)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)
ACTION_META = {"argos_risk": "destructive"}

server = MCPServer(name="portainer", version=VERSION)


def _configured() -> str | None:
    if not os.environ.get("ARGOS_PORTAINER_URL") or not os.environ.get("ARGOS_PORTAINER_KEY"):
        return (
            "Portainer no está configurado: falta url o api_key en secrets/inventory.yaml "
            "(servicio 'portainer'). Pídeselo al usuario o revisa el inventario."
        )
    return None


def _client() -> PortainerClient:
    return PortainerClient(
        os.environ["ARGOS_PORTAINER_URL"],
        os.environ["ARGOS_PORTAINER_KEY"],
        endpoint=int(os.environ.get("ARGOS_PORTAINER_ENDPOINT", "1")),
        verify=os.environ.get("ARGOS_PORTAINER_VERIFY", "1") != "0",
    )


def _dry_run() -> bool:
    return os.environ.get("ARGOS_PORTAINER_DRY_RUN", "0") != "0"


async def _read(fn_name: str, *args) -> str:
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    client = _client()
    try:
        result = await getattr(client, fn_name)(*args)
        return (
            json.dumps(result, ensure_ascii=False, indent=2)[:12000]
            if not isinstance(result, str)
            else result[:12000]
        )
    except PortainerError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def list_containers() -> str:
    """Lista los contenedores del entorno Portainer configurado (nombre, estado, imagen)."""
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    client = _client()
    try:
        data = await client.containers()
        brief = [
            {
                "id": c.get("Id", "")[:12],
                "names": c.get("Names"),
                "image": c.get("Image"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
            for c in data
        ]
        return json.dumps(brief, ensure_ascii=False, indent=2)[:12000]
    except PortainerError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=READ)
async def inspect_container(container_id: str) -> str:
    """Inspecciona un contenedor por id o nombre."""
    return await _read("inspect", container_id)


@server.tool(annotations=READ)
async def container_logs(container_id: str, tail: int = 200) -> str:
    """Devuelve las últimas líneas de log de un contenedor."""
    return await _read("logs", container_id, tail)


@server.tool(annotations=READ)
async def list_stacks() -> str:
    """Lista los stacks definidos en Portainer."""
    return await _read("stacks")


@server.tool(annotations=READ)
async def list_endpoints() -> str:
    """Lista los entornos (endpoints) que gestiona Portainer."""
    return await _read("endpoints")


async def _action(container_id: str, action: str) -> str:
    if msg := _configured():
        return f"NO CONFIGURADO: {msg}"
    if _dry_run():
        return f"[dry-run] no se ejecuta. Acción prevista: {action} sobre {container_id}."
    client = _client()
    try:
        await client.container_action(container_id, action)
        return f"{action} ejecutado sobre {container_id}."
    except PortainerError as exc:
        return f"ERROR: {exc}"
    finally:
        await client.aclose()


@server.tool(annotations=ACTION, meta=ACTION_META)
async def start_container(container_id: str) -> str:
    """Arranca un contenedor (requiere aprobación)."""
    return await _action(container_id, "start")


@server.tool(annotations=ACTION, meta=ACTION_META)
async def stop_container(container_id: str) -> str:
    """Detiene un contenedor (requiere aprobación)."""
    return await _action(container_id, "stop")


@server.tool(annotations=ACTION, meta=ACTION_META)
async def restart_container(container_id: str) -> str:
    """Reinicia un contenedor (requiere aprobación)."""
    return await _action(container_id, "restart")


if __name__ == "__main__":  # pragma: no cover
    server.run("stdio")
