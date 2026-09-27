"""Adaptador MCP (stdio) → tools de Argos (RF-09, RF-SK-02).

Cada servidor MCP se lanza como subproceso con su propio entorno (secretos scoped, RF-SEC-01).
Las anotaciones MCP se traducen a la metadata de riesgo/idempotencia de RF-21; una tool sin
anotaciones se trata con el valor más conservador (escritura, no idempotente).
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
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
                self._remote_name, args, read_timeout_seconds=600)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"MCP {self.name}: {exc}", ErrorKind.TOOL_ERROR) from exc
        text = "\n".join(
            getattr(c, "text", "") for c in (getattr(result, "content", None) or [])).strip()
        if getattr(result, "is_error", False):
            raise ToolError(text or f"MCP {self.name} devolvió error", ErrorKind.TOOL_ERROR)
        return ToolResult(text)


class McpConnections:
    """Mantiene abiertas las conexiones MCP durante una sesión."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()

    async def connect(self, server: McpServerSpec) -> list[McpTool]:
        params = StdioServerParameters(command=server.command, args=server.args, env=server.env)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        init = await session.initialize()
        version = getattr(getattr(init, "server_info", None), "version", None) or "unknown"
        listed = await session.list_tools()
        return [McpTool(server, version, session, t) for t in listed.tools]

    async def aclose(self) -> None:
        await self._stack.aclose()


def reminders_server(db_path: str) -> McpServerSpec:
    # El SDK MCP solo hereda un entorno mínimo (PATH, HOME...): se pasa PYTHONPATH explícito para
    # que el servidor encuentre `argos` también sin instalación editable (p. ej. en contenedor).
    src_dir = str(Path(__file__).resolve().parents[2])
    return McpServerSpec(
        name="reminders", command=sys.executable,
        args=["-m", "argos.mcp_servers.reminders.server"],
        env={"ARGOS_REMINDERS_DB": db_path, "PYTHONPATH": src_dir})


def kali_server(url: str, token: str | None, scope: list[str], authorization_ref: str | None,
                dry_run: bool) -> McpServerSpec:
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
    return McpServerSpec(name="kali", command=sys.executable,
                         args=["-m", "argos.mcp_servers.kali.server"], env=env)


def portainer_server(url: str, api_key: str, endpoint: int, dry_run: bool,
                     verify: bool = True) -> McpServerSpec:
    """MCP de Portainer (UC-3). URL y api key vienen del inventario, nunca del modelo."""
    return McpServerSpec(
        name="portainer", command=sys.executable,
        args=["-m", "argos.mcp_servers.portainer.server"],
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[2]),
             "ARGOS_PORTAINER_URL": url or "", "ARGOS_PORTAINER_KEY": api_key or "",
             "ARGOS_PORTAINER_ENDPOINT": str(endpoint), "ARGOS_PORTAINER_VERIFY": "1" if verify
             else "0", "ARGOS_PORTAINER_DRY_RUN": "1" if dry_run else "0"})
