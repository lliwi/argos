"""Adaptador MCP (stdio) → tools de Argos (RF-09, RF-SK-02).

Cada servidor MCP se lanza como subproceso con su propio entorno (secretos scoped, RF-SEC-01).
Las anotaciones MCP se traducen a la metadata de riesgo/idempotencia de RF-21; una tool sin
anotaciones se trata con el valor más conservador (escritura, no idempotente).
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from argos.audit.events import ErrorKind, RiskClass
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult


@dataclass
class McpServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def risk_from_annotations(ann: Any) -> tuple[RiskClass, bool]:
    if ann is None:
        return RiskClass.WRITE, False
    idempotent = bool(getattr(ann, "idempotent_hint", False))
    if getattr(ann, "read_only_hint", False):
        return RiskClass.READ, True
    if getattr(ann, "destructive_hint", None):
        return RiskClass.DESTRUCTIVE, idempotent
    return RiskClass.WRITE, idempotent


class McpTool(Tool):
    def __init__(self, server: McpServerSpec, version: str, session: ClientSession, spec: Any):
        self._session = session
        self._remote_name = spec.name
        self.name = f"{server.name}.{spec.name}"
        self.version = version
        self.mcp_server = server.name
        self.description = spec.description or spec.name
        self.parameters = spec.input_schema or {"type": "object", "properties": {}}
        self.risk_class, self.idempotent = risk_from_annotations(spec.annotations)
        # Una tool MCP puede declararse explícitamente ofensiva vía meta (p. ej. el MCP de Kali):
        # eso fuerza aprobación humana en Argos (RF-GOV-04).
        meta = getattr(spec, "meta", None) or {}
        if isinstance(meta, dict) and meta.get("argos_risk") == "offensive":
            self.risk_class = RiskClass.OFFENSIVE

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # El MCP de Kali gestiona su propio dry-run (devuelve el comando previsto y valida el
        # alcance incluso en simulación); el resto de MCP no ejecuta en dry-run.
        if ctx.dry_run and self.risk_class != RiskClass.READ and self.mcp_server != "kali":
            return ToolResult(f"[dry-run] se invocaría {self.name}({args})")
        try:
            result = await self._session.call_tool(
                self._remote_name, args, read_timeout_seconds=600
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"MCP {self.name}: {exc}", ErrorKind.TOOL_ERROR) from exc
        text = "\n".join(
            getattr(c, "text", "") for c in (getattr(result, "content", None) or [])
        ).strip()
        if getattr(result, "is_error", False):
            raise ToolError(text or f"MCP {self.name} devolvió error", ErrorKind.TOOL_ERROR)
        return ToolResult(text)


class McpConnections:
    """Mantiene abiertas las conexiones MCP durante una sesión.

    Cada conexión vive en su propia tarea "propietaria", que entra y sale de los contextos de
    stdio_client/ClientSession (anyio exige hacerlo en la misma tarea). La sesión solo le pide que
    pare: así una cancelación de la sesión (o una segunda, en el apagado del núcleo) nunca cae
    dentro de la salida de stdio_client, donde anyio se queda en bucle.
    """

    CLOSE_TIMEOUT_S = 15

    def __init__(self) -> None:
        self._conns: list[tuple[asyncio.Event, asyncio.Task[None]]] = []

    async def connect(self, server: McpServerSpec) -> list[McpTool]:
        params = StdioServerParameters(command=server.command, args=server.args, env=server.env)
        ready: asyncio.Future[tuple[ClientSession, Any, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        stop = asyncio.Event()

        async def owner() -> None:
            try:
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    init = await session.initialize()
                    listed = await session.list_tools()
                    ready.set_result((session, init, listed))
                    await stop.wait()
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except BaseException as exc:
                if ready.done():
                    raise
                ready.set_exception(exc)

        task = asyncio.create_task(owner(), name=f"mcp-{server.name}")
        self._conns.append((stop, task))
        session, init, listed = await ready
        version = getattr(getattr(init, "server_info", None), "version", None) or "unknown"
        return [McpTool(server, version, session, t) for t in listed.tools]

    async def aclose(self) -> None:
        conns, self._conns = self._conns, []
        if not conns:
            return
        for stop, _ in conns:
            stop.set()
        tasks = [t for _, t in conns]
        waiter = asyncio.ensure_future(asyncio.wait(tasks, timeout=self.CLOSE_TIMEOUT_S))
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            await waiter  # los subprocesos se cierran igualmente; luego se propaga
            raise
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            for t in tasks:
                if t.done() and not t.cancelled():
                    t.exception()  # marca la excepción como recuperada (sin avisos al GC)


def reminders_server(db_path: str) -> McpServerSpec:
    # El SDK MCP solo hereda un entorno mínimo (PATH, HOME...): se pasa PYTHONPATH explícito para
    # que el servidor encuentre `argos` también sin instalación editable (p. ej. en contenedor).
    src_dir = str(Path(__file__).resolve().parents[2])
    return McpServerSpec(
        name="reminders",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.reminders.server"],
        env={"ARGOS_REMINDERS_DB": db_path, "PYTHONPATH": src_dir},
    )


def kali_server(
    url: str, token: str | None, scope: list[str], authorization_ref: str | None, dry_run: bool
) -> McpServerSpec:
    """MCP de Kali (UC-2). El alcance y la autorización se inyectan por entorno; el servidor los
    aplica antes de cada acción (RF-SEC-06, RF-LEG-01)."""
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "ARGOS_KALI_URL": url,
        "ARGOS_PENTEST_SCOPE": ",".join(scope),
        "ARGOS_PENTEST_AUTH": authorization_ref or "",
        "ARGOS_PENTEST_DRY_RUN": "1" if dry_run else "0",
    }
    if token:
        env["ARGOS_KALI_TOKEN"] = token
    return McpServerSpec(
        name="kali", command=sys.executable, args=["-m", "argos.mcp_servers.kali.server"], env=env
    )


def portainer_server(
    url: str, api_key: str, endpoint: int, dry_run: bool, verify: bool = True
) -> McpServerSpec:
    """MCP de Portainer (UC-3). URL y api key vienen del inventario, nunca del modelo."""
    return McpServerSpec(
        name="portainer",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.portainer.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_PORTAINER_URL": url or "",
            "ARGOS_PORTAINER_KEY": api_key or "",
            "ARGOS_PORTAINER_ENDPOINT": str(endpoint),
            "ARGOS_PORTAINER_VERIFY": "1" if verify else "0",
            "ARGOS_PORTAINER_DRY_RUN": "1" if dry_run else "0",
        },
    )


def homeassistant_server(url: str, token: str, dry_run: bool, verify: bool = True) -> McpServerSpec:
    """MCP de Home Assistant (UC-3). URL y token vienen del inventario, nunca del modelo."""
    return McpServerSpec(
        name="homeassistant",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.homeassistant.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_HA_URL": url or "",
            "ARGOS_HA_TOKEN": token or "",
            "ARGOS_HA_VERIFY": "1" if verify else "0",
            "ARGOS_HA_DRY_RUN": "1" if dry_run else "0",
        },
    )


def media_server(
    jackett_url: str,
    jackett_key: str,
    transmission_url: str,
    tr_user: str,
    tr_pass: str,
    dry_run: bool,
) -> McpServerSpec:
    """MCP de descargas (Jackett + Transmission, UC-3). URLs y api key del inventario."""
    return McpServerSpec(
        name="media",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.media.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_JACKETT_URL": jackett_url or "",
            "ARGOS_JACKETT_KEY": jackett_key or "",
            "ARGOS_TRANSMISSION_URL": transmission_url or "",
            "ARGOS_TRANSMISSION_USER": tr_user or "",
            "ARGOS_TRANSMISSION_PASS": tr_pass or "",
            "ARGOS_MEDIA_DRY_RUN": "1" if dry_run else "0",
        },
    )


def nas_server(host: str, user: str, password: str, community: str, dry_run: bool) -> McpServerSpec:
    """MCP del NAS (SMB + SNMP, UC-3). Credenciales del inventario, nunca del modelo."""
    return McpServerSpec(
        name="nas",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.nas.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_NAS_HOST": host or "",
            "ARGOS_NAS_USER": user or "",
            "ARGOS_NAS_PASSWORD": password or "",
            "ARGOS_NAS_COMMUNITY": community or "",
            "ARGOS_NAS_DRY_RUN": "1" if dry_run else "0",
        },
    )


def cloudflare_server(token: str, dry_run: bool) -> McpServerSpec:
    """MCP de Cloudflare (DNS, túneles, analítica; UC-3). Token del inventario, nunca del modelo."""
    return McpServerSpec(
        name="cloudflare",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.cloudflare.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_CF_TOKEN": token or "",
            "ARGOS_CF_DRY_RUN": "1" if dry_run else "0",
        },
    )


def weather_server(city: str = "") -> McpServerSpec:
    """MCP de meteorología (eltiempo.es, UC-4). Solo lectura, sin credenciales."""
    return McpServerSpec(
        name="weather",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.weather.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_WEATHER_CITY": city or "",
        },
    )


def osint_server(url: str, api_key: str) -> McpServerSpec:
    """MCP del backend OSINT propio (UC-1). URL y api key del inventario, nunca del modelo."""
    return McpServerSpec(
        name="osint",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.osint.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_OSINT_URL": url or "",
            "ARGOS_OSINT_KEY": api_key or "",
        },
    )


def notion_server(token: str, dry_run: bool) -> McpServerSpec:
    """MCP de Notion (UC-4). Token del inventario, nunca del modelo."""
    return McpServerSpec(
        name="notion",
        command=sys.executable,
        args=["-m", "argos.mcp_servers.notion.server"],
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "ARGOS_NOTION_TOKEN": token or "",
            "ARGOS_NOTION_DRY_RUN": "1" if dry_run else "0",
        },
    )
