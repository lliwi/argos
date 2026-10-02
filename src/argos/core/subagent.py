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
        "type": "object",
        "required": ["task"],
        "properties": {
            "task": {"type": "string", "description": "Subtarea autocontenida y verificable"},
            "profile": {
                "type": "string",
                "description": "Perfil especialista al que delegar (ver descripción)",
            },
            "budget_tokens": {"type": "integer", "description": "Tope opcional de tokens"},
        },
    }
    risk_class = RiskClass.WRITE
    idempotent = False

    def __init__(
        self,
        spawn: Spawner,
        default_budget: int,
        own_profile: str,
        delegate_profiles: dict[str, str] | None = None,
        isolated_profiles: dict[str, str] | None = None,
    ) -> None:
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
                "Devuelve solo el resultado. Enruta cada tarea al especialista adecuado."
            )
            # Perfiles de otros segmentos (ADR-0026): NO se delegan en ellos (P2); se les pasa la
            # tarea con `agent.handoff`, que no devuelve su resultado (va al usuario).
            if isolated_profiles:
                aislados = "; ".join(f"{n}: {d}" for n, d in isolated_profiles.items())
                self.description += (
                    f" Para estos otros perfiles, AISLADOS en su propio núcleo, NO uses delegate: "
                    f"usa `agent.handoff` → {aislados}."
                )
        else:
            self.description = (
                "Delega una subtarea acotada a un subagente con contexto limpio (mismo perfil y "
                "workspace). Devuelve solo su resultado final. Útil para procesar algo voluminoso "
                "sin llenar tu contexto."
            )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = str(args.get("task", "")).strip()
        if not task:
            raise ToolError("task vacía", ErrorKind.VALIDATION_ERROR)
        target = str(args.get("profile") or "").strip() or None
        if target and target != self._own and target not in self._targets:
            raise ToolError(
                f"no puedes delegar al perfil {target!r}. Permitidos: "
                f"{sorted(self._targets) or '(ninguno)'}",
                ErrorKind.VALIDATION_ERROR,
            )
        budget = int(args.get("budget_tokens") or self._default_budget)
        budget = max(1, min(budget, self._default_budget))
        sid, status, message, steps, tokens = await self._spawn(task, budget, target)
        output = f"[subagente {sid[:12]} · {status} · {steps} pasos · {tokens} tokens]\n{message}"
        return ToolResult(
            output,
            ok=status == "completed",
            error_kind=None if status == "completed" else ErrorKind.TOOL_ERROR,
            data={"subagent_session": sid, "subagent_tokens": tokens},
        )


class HandoffTool(Tool):
    """Pasa la tarea a un perfil de OTRO segmento (osint, pentest), aislado del orquestador.

    No ejecuta ni devuelve el resultado: emite un evento `handoff` que el canal (chat/Matrix)
    reenvía al daemon de ese segmento; el resultado llega al usuario, nunca al contexto del
    orquestador (P2, ADR-0026). Para perfiles del mismo segmento, usa `agent.delegate`.
    """

    name = "agent.handoff"
    risk_class = RiskClass.READ
    idempotent = False
    parameters = {
        "type": "object",
        "required": ["profile", "task"],
        "properties": {
            "profile": {
                "type": "string",
                "description": "Perfil aislado destino (ver descripción)",
            },
            "task": {
                "type": "string",
                "description": "La tarea del usuario, literal: el destino la ejecuta por su cuenta",
            },
        },
    }

    def __init__(self, isolated_profiles: dict[str, str], segment_of: Callable[[str], str]) -> None:
        self._targets = isolated_profiles
        self._segment_of = segment_of
        catalogo = "; ".join(f"{n}: {d}" for n, d in isolated_profiles.items())
        self.description = (
            "Pasa la tarea del usuario a un perfil AISLADO de otro segmento y termina: su "
            f"resultado va directo al usuario, tú NO lo ves. Destinos → {catalogo}. Úsalo cuando "
            "el usuario pida una auditoría/pentest (pentest) o investigación OSINT (osint); pásale "
            "su petición tal cual (incluidos objetivo y cualquier dato que haya dado)."
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from argos.audit.events import HandoffRequested

        task = str(args.get("task", "")).strip()
        target = str(args.get("profile") or "").strip()
        if not task:
            raise ToolError("task vacía", ErrorKind.VALIDATION_ERROR)
        if target not in self._targets:
            raise ToolError(
                f"no puedes pasar la tarea a {target!r}. Perfiles aislados: "
                f"{sorted(self._targets) or '(ninguno)'}",
                ErrorKind.VALIDATION_ERROR,
            )
        ctx.emit(
            HandoffRequested(
                session_id=ctx.session_id,
                target_profile=target,
                target_segment=self._segment_of(target),
                task=task,
            )
        )
        return ToolResult(
            f"Tarea pasada al perfil {target} (núcleo aislado). El resultado le llegará "
            "directamente al usuario; no esperes su salida. Termina ya con un aviso breve.",
            data={"handoff_profile": target},
        )
