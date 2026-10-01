"""Scratchpad externo (RF-CTX-07): plan, pendientes y hallazgos fuera del transcript.

Vive en `<workspace>/.argos/scratchpad.md` (fuera de `out/`), así sobrevive a la poda de contexto
y se relee a demanda. Escribir la sección `plan` emite `plan_event` (RF-OB-02).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from argos.audit.events import ErrorKind, PlanEvent, RiskClass
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

SECTIONS = ("plan", "todo", "findings")
MAX_SECTION = 6000


def _path(ctx: ToolContext) -> Path:
    return ctx.workspace / ".argos" / "scratchpad.md"


def read_sections(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    parts = re.split(r"^## (\w+)\n", path.read_text(encoding="utf-8"), flags=re.M)
    return {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


class ScratchpadWrite(Tool):
    name = "scratchpad.write"
    description = (
        "Guarda tu plan, pendientes o hallazgos (sección: plan | todo | findings). "
        "Reemplaza la sección. Útil en tareas largas: el historial se poda, esto no."
    )
    parameters = {
        "type": "object",
        "required": ["section", "content"],
        "properties": {
            "section": {"type": "string", "enum": list(SECTIONS)},
            "content": {"type": "string"},
        },
    }
    risk_class = RiskClass.READ  # solo escribe estado propio del agente, sin efectos externos
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        section = str(args.get("section", ""))
        if section not in SECTIONS:
            raise ToolError(f"sección inválida: {section!r}", ErrorKind.VALIDATION_ERROR)
        content = str(args.get("content", ""))[:MAX_SECTION]
        path = _path(ctx)
        sections = read_sections(path)
        previous = sections.get(section)
        sections[section] = content
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(f"## {k}\n{sections[k]}\n\n" for k in SECTIONS if k in sections),
            encoding="utf-8",
        )
        if section == "plan":
            ctx.emit(
                PlanEvent(
                    session_id=ctx.session_id,
                    turn_id=ctx.turn_id,
                    plan_type="revise" if previous else "create",
                    summary=content[:500],
                )
            )
        return ToolResult(f"sección {section} guardada ({len(content)} caracteres)")


class ScratchpadRead(Tool):
    name = "scratchpad.read"
    description = "Relee tu scratchpad (plan, pendientes, hallazgos)."
    parameters = {"type": "object", "properties": {}}
    risk_class = RiskClass.READ
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _path(ctx)
        return ToolResult(path.read_text(encoding="utf-8") if path.exists() else "(vacío)")


SCRATCHPAD_TOOLS = (ScratchpadWrite, ScratchpadRead)
