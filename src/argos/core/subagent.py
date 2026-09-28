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
Spawner = Callable[[str, int, "str | None"], Awaitable[tuple[str, str, str, int, int]]]


class DelegateTool(Tool):
    name = "agent.delegate"
    parameters = {
        "type": "object", "required": ["task"],
        "properties": {
            "task": {"type": "string", "description": "Subtarea autocontenida y verificable"},
            "profile": {"type": "string",
                        "description": "Perfil especialista al que delegar (ver descripción)"},
            "budget_tokens": {"type": "integer", "description": "Tope opcional de tokens"},
        },
    }
    risk_class = RiskClass.WRITE
    idempotent = False

    def __init__(self, spawn: Spawner, default_budget: int, own_profile: str,
                 delegate_profiles: dict[str, str] | None = None) -> None:
        self._spawn = spawn
        self._default_budget = default_budget
        self._own = own_profile
        # {perfil: descripción de sus capacidades} — los perfiles a los que se puede delegar.
        self._targets = delegate_profiles or {}
        if self._targets:
            catalogo = "; ".join(f"{n}: {d}" for n, d in self._targets.items())
            self.description = (
                "Delega una subtarea a un subagente especialista con contexto limpio, indicando "
                f"su `profile`. Especialistas disponibles → {catalogo}. Cada uno tiene sus "
                "herramientas, credenciales y controles (aprobación humana donde aplique). "
                "Devuelve solo el resultado. Enruta cada tarea al especialista adecuado.")
        else:
            self.description = (
                "Delega una subtarea acotada a un subagente con contexto limpio (mismo perfil y "
                "workspace). Devuelve solo su resultado final. Útil para procesar algo voluminoso "
                "sin llenar tu contexto.")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = str(args.get("task", "")).strip()
        if not task:
            raise ToolError("task vacía", ErrorKind.VALIDATION_ERROR)
        target = str(args.get("profile") or "").strip() or None
        if target and target != self._own and target not in self._targets:
            raise ToolError(
                f"no puedes delegar al perfil {target!r}. Permitidos: "
                f"{sorted(self._targets) or '(ninguno)'}", ErrorKind.VALIDATION_ERROR)
        budget = int(args.get("budget_tokens") or self._default_budget)
        budget = max(1, min(budget, self._default_budget))
        sid, status, message, steps, tokens = await self._spawn(task, budget, target)
        output = (f"[subagente {sid[:12]} · {status} · {steps} pasos · {tokens} tokens]\n"
                  f"{message}")
        return ToolResult(output, ok=status == "completed",
                          error_kind=None if status == "completed" else ErrorKind.TOOL_ERROR,
                          data={"subagent_session": sid, "subagent_tokens": tokens})
