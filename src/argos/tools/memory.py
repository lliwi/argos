"""Tools de memoria durable (RF-16/17). Lo que guarda el agente tiene procedencia `agent`:
vuelve a futuras sesiones como dato <untrusted>, nunca como instrucción (ver argos.state)."""

from __future__ import annotations

from typing import Any

from argos.audit.events import ErrorKind, MemoryEvent, RiskClass
from argos.state import KINDS, StateStore, now_iso
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult


class MemorySave(Tool):
    name = "memory.save"
    description = (
        "Guarda un dato duradero útil para futuras conversaciones (hechos del "
        "entorno, preferencias expresadas por el usuario, hallazgos). No guardes "
        "secretos ni datos personales de terceros innecesarios."
    )
    parameters = {
        "type": "object",
        "required": ["content"],
        "properties": {
            "content": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "tags": {"type": "string"},
        },
    }
    risk_class = RiskClass.WRITE
    idempotent = True  # deduplica por contenido

    def __init__(self, state: StateStore, ttl_days: int | None = None) -> None:
        self._state = state
        self._ttl = ttl_days

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Un secreto conocido nunca llega a la memoria (se guardaría fuera del vault).
        content = ctx.store.redactor.redact_secrets(str(args.get("content", "")))
        try:
            mem = self._state.add_memory(
                ctx.profile.name,
                str(args.get("kind") or "fact"),
                content,
                "agent",
                tags=str(args.get("tags") or ""),
                source_session=ctx.session_id,
                ttl_days=self._ttl,
            )
        except ValueError as exc:
            raise ToolError(str(exc), ErrorKind.VALIDATION_ERROR) from exc
        ctx.emit(
            MemoryEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                op="save",
                memory_ids=[mem.id],
                detail=f"{mem.kind}: {mem.content[:200]}",
            )
        )
        return ToolResult(f"guardado como {mem.id} ({mem.kind})")


class MemorySearch(Tool):
    name = "memory.search"
    description = "Busca en la memoria durable por palabras clave."
    parameters = {
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
    }
    risk_class = RiskClass.READ
    idempotent = True

    def __init__(self, state: StateStore) -> None:
        self._state = state

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        found = self._state.search(ctx.profile.name, str(args.get("query", "")), limit=8)
        ctx.emit(
            MemoryEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                op="search",
                memory_ids=[m.id for m in found],
                detail=str(args.get("query")),
            )
        )
        if not found:
            return ToolResult("(sin resultados)")
        return ToolResult(
            "\n".join(f"- [{m.kind} · {m.provenance} · {m.id}] {m.content}" for m in found)
        )


class MemoryUpdate(Tool):
    name = "memory.update"
    description = (
        "Corrige una nota tuya de memoria (por su id) cuando un dato nuevo la "
        "contradice o la completa. No puedes modificar lo que guardó el usuario."
    )
    parameters = {
        "type": "object",
        "required": ["id", "content"],
        "properties": {"id": {"type": "string"}, "content": {"type": "string"}},
    }
    risk_class = RiskClass.WRITE
    idempotent = True

    def __init__(self, state: StateStore) -> None:
        self._state = state

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        mid = str(args.get("id", ""))
        try:
            current = self._state.memory(mid)
        except KeyError as exc:
            raise ToolError(f"no existe la memoria {mid}", ErrorKind.VALIDATION_ERROR) from exc
        if current.profile != ctx.profile.name:
            raise ToolError(f"no existe la memoria {mid}", ErrorKind.VALIDATION_ERROR)
        if current.provenance != "agent":
            raise ToolError(
                "esa memoria la guardó el usuario: pídele que la edite él",
                ErrorKind.VALIDATION_ERROR,
            )
        content = ctx.store.redactor.redact_secrets(str(args.get("content", "")))
        # Se reescribe como nota del agente (no pasa a ser del usuario: no la ha revisado nadie).
        with self._state._lock:
            self._state.db.execute(
                "UPDATE memories SET content=?, updated_at=? WHERE id=?",
                (content.strip(), now_iso(), mid),
            )
            self._state.db.commit()
        ctx.emit(
            MemoryEvent(
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                op="update",
                memory_ids=[mid],
                detail=content[:200],
            )
        )
        return ToolResult(f"memoria {mid} corregida")
