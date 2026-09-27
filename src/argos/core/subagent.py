"""Subagentes con contexto limpio (RF-02, RF-CTX-04).

`agent.delegate` lanza una sesión hija con contexto fresco sobre el mismo workspace y devuelve al
padre solo el resultado destilado (su mensaje final). La relación padre-hijo queda en auditoría
(`parent_session_id`, RF-OB-03) y los tokens del hijo se cargan al presupuesto del padre.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from argos.audit.events import ErrorKind, RiskClass
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

# (tarea, presupuesto de tokens) -> (session_id, status, message, steps, tokens)
Spawner = Callable[[str, int], Awaitable[tuple[str, str, str, int, int]]]


class DelegateTool(Tool):
    name = "agent.delegate"
    description = (
        "Delega una subtarea acotada a un subagente con contexto limpio (mismo perfil y "
        "workspace). Devuelve solo su resultado final. Úsalo para investigar o procesar algo "
        "voluminoso sin llenar tu contexto.")
    parameters = {
        "type": "object", "required": ["task"],
        "properties": {
            "task": {"type": "string", "description": "Subtarea autocontenida y verificable"},
            "budget_tokens": {"type": "integer", "description": "Tope opcional de tokens"},
        },
    }
    risk_class = RiskClass.WRITE
    idempotent = False

    def __init__(self, spawn: Spawner, default_budget: int) -> None:
        self._spawn = spawn
        self._default_budget = default_budget

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = str(args.get("task", "")).strip()
        if not task:
            raise ToolError("task vacía", ErrorKind.VALIDATION_ERROR)
        budget = int(args.get("budget_tokens") or self._default_budget)
        budget = max(1, min(budget, self._default_budget))
        sid, status, message, steps, tokens = await self._spawn(task, budget)
        output = (f"[subagente {sid[:12]} · {status} · {steps} pasos · {tokens} tokens]\n"
                  f"{message}")
        return ToolResult(output, ok=status == "completed",
                          error_kind=None if status == "completed" else ErrorKind.TOOL_ERROR,
                          data={"subagent_session": sid, "subagent_tokens": tokens})
