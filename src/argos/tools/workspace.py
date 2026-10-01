"""Tools de workspace: lectura en `in/` y `out/`, escritura solo en `out/` (RF-EX-03).

Cada acceso emite un `file_event` con tamaño y hash (RF-OB-02).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from argos.audit.events import ErrorKind, FileEvent, RiskClass
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

MAX_READ = 200_000


def resolve(workspace: Path, rel: str, *, writable: bool) -> Path:
    """Resuelve `in/x` o `out/x` dentro del workspace; rechaza traversal y zona incorrecta."""
    rel = rel.strip().removeprefix("/workspace/").lstrip("/")
    if not rel.startswith(("in/", "out/")) and rel not in ("in", "out"):
        rel = f"out/{rel}"
    target = (workspace / rel).resolve()
    zone = (workspace / ("out" if writable else "")).resolve()
    if not target.is_relative_to(zone) or not target.is_relative_to(workspace.resolve()):
        raise ToolError(f"ruta fuera de la zona permitida: {rel}", ErrorKind.VALIDATION_ERROR)
    return target


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReadFile(Tool):
    name = "workspace.read_file"
    description = "Lee un fichero de texto del workspace (rutas 'in/...' o 'out/...')."
    parameters = {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
    }
    risk_class = RiskClass.READ
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve(ctx.workspace, str(args.get("path", "")), writable=False)
        if not path.is_file():
            raise ToolError(f"no existe: {args.get('path')}")
        data = path.read_bytes()[:MAX_READ]
        ctx.emit(
            FileEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                path=str(path.relative_to(ctx.workspace)),
                op="read",
                bytes=len(data),
                hash=_sha(data),
            )
        )
        return ToolResult(data.decode("utf-8", errors="replace"))


class WriteFile(Tool):
    name = "workspace.write_file"
    description = "Escribe (sobrescribe) un fichero de texto en 'out/...'."
    parameters = {
        "type": "object",
        "required": ["path", "content"],
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    }
    risk_class = RiskClass.WRITE
    idempotent = True  # escribir el mismo contenido dos veces tiene el mismo efecto

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = resolve(ctx.workspace, str(args.get("path", "")), writable=True)
        data = str(args.get("content", "")).encode("utf-8")
        rel = str(path.relative_to(ctx.workspace))
        if ctx.dry_run:
            return ToolResult(f"[dry-run] se escribirían {len(data)} bytes en {rel}")
        op = "write" if path.exists() else "create"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        ctx.emit(
            FileEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                path=rel,
                op=op,
                bytes=len(data),
                hash=_sha(data),
            )
        )
        return ToolResult(f"escritos {len(data)} bytes en {rel}")


class ListFiles(Tool):
    name = "workspace.list"
    description = "Lista ficheros del workspace ('in' u 'out')."
    parameters = {"type": "object", "properties": {"path": {"type": "string", "default": "out"}}}
    risk_class = RiskClass.READ
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        base = resolve(ctx.workspace, str(args.get("path") or "out"), writable=False)
        if not base.is_dir():
            raise ToolError(f"no es un directorio: {args.get('path')}")
        entries = sorted(
            f"{p.relative_to(ctx.workspace)}{'/' if p.is_dir() else f' ({p.stat().st_size} B)'}"
            for p in base.rglob("*")
            if ".." not in p.parts
        )[:500]
        ctx.emit(
            FileEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                path=str(base.relative_to(ctx.workspace)),
                op="list",
            )
        )
        return ToolResult("\n".join(entries) or "(vacío)")


WORKSPACE_TOOLS = (ReadFile, WriteFile, ListFiles)
