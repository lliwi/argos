"""Gestor de contexto (§14).

- Prefijo estable: system + tools no cambian entre turnos (RF-CTX-01).
- Observaciones grandes se truncan y se referencian por hash; `context.read_ref` las pagina
  bajo demanda (RF-CTX-03).
- Intentos fallidos antiguos se podan a una línea (RF-CTX-06).
- Todo resultado de tool se marca como <untrusted>: datos, nunca instrucciones (P2, RF-SEC-07).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argos.audit.events import RiskClass
from argos.audit.store import AuditStore
from argos.model.base import Decision, Message, ModelRequest, ToolSpec
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult


@dataclass
class _Step:
    decision_json: str
    observation: str
    failed: bool
    summary: str


@dataclass
class ContextManager:
    system: str
    tools: list[ToolSpec]
    task: str
    store: AuditStore
    max_chars: int = 4000
    prune_failed_after: int = 2
    steps: list[_Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # RF-10: con carga diferida, las tools no cargadas aparecen solo con nombre y descripción.
    lazy_tools: bool = False
    always_loaded: frozenset[str] = frozenset({"tools.load"})
    loaded: set[str] = field(default_factory=set)
    # Skills activadas: contenido de confianza, va al sistema y no se poda (§9).
    skills_loaded: dict[str, str] = field(default_factory=dict)
    # Imágenes adjuntas por el usuario: acompañan a cada petición (el modelo no guarda estado).
    images: list[Path] = field(default_factory=list)

    def add_skill(self, name: str, body: str) -> None:
        self.skills_loaded[name] = body

    def system_text(self) -> str:
        text = self.system
        for name, body in self.skills_loaded.items():
            text += f"\n\n## Skill activa: {name}\n{body}"
        return text

    def load_tools(self, names: list[str]) -> tuple[list[str], list[str]]:
        known = {t.name for t in self.tools}
        ok = [n for n in names if n in known]
        self.loaded.update(ok)
        return ok, [n for n in names if n not in known]

    def visible_tools(self) -> list[ToolSpec]:
        if not self.lazy_tools:
            return self.tools
        return [
            t
            if t.name in self.loaded or t.name in self.always_loaded
            else ToolSpec(t.name, t.description, None)
            for t in self.tools
        ]

    def add_step(
        self,
        decision: Decision,
        result: ToolResult | None,
        raw_observation: str,
        failed: bool,
        summary: str | None = None,
        ref: str | None = None,
    ) -> None:
        decision_json = json.dumps(
            {"type": decision.type, "tool": decision.tool, "args": decision.args},
            ensure_ascii=False,
        )
        # RNF-06: ningún secreto conocido entra en el contexto, aunque una tool lo imprima.
        raw_observation = self.store.redactor.redact_secrets(raw_observation)
        summary = self.store.redactor.redact_secrets(summary) if summary else summary
        if summary and ref:
            # RF-CTX-03: resumen + referencia; el detalle se pagina con context.read_ref.
            observation = (
                f"<untrusted>\n[resumen de {len(raw_observation)} caracteres; detalle "
                f"con context.read_ref ref={ref}]\n{summary[:2000]}\n</untrusted>"
            )
        else:
            observation = self._fit(raw_observation)
        summary = f"{decision.tool}({_short(decision.args)}) → fallo"
        self.steps.append(_Step(decision_json, observation, failed, summary))

    def add_note(self, text: str) -> None:
        """Observación del sistema (no de una tool): formato inválido, denegaciones, avisos."""
        text = self.store.redactor.redact_secrets(text)
        self.steps.append(_Step("", f"[sistema] {text}", failed=True, summary=text[:120]))

    def _fit(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return f"<untrusted>\n{text}\n</untrusted>"
        ref = self.store.put_blob(text)
        head = text[: self.max_chars * 2 // 3]
        tail = text[-self.max_chars // 3 :]
        return (
            f"<untrusted>\n{head}\n[... {len(text) - len(head) - len(tail)} caracteres "
            f"omitidos; usa context.read_ref con ref={ref} para paginar ...]\n{tail}\n"
            f"</untrusted>"
        )

    def build(self) -> ModelRequest:
        messages = [Message("user", self.task)]
        failed_idx = [i for i, s in enumerate(self.steps) if s.failed]
        prunable = set(
            failed_idx[: -self.prune_failed_after] if self.prune_failed_after else failed_idx
        )
        for i, step in enumerate(self.steps):
            if i in prunable:
                messages.append(Message("observation", f"(intento fallido podado: {step.summary})"))
                continue
            if step.decision_json:
                messages.append(Message("assistant", step.decision_json))
            messages.append(Message("observation", step.observation))
        return ModelRequest(
            system=self.system_text(),
            tools=self.visible_tools(),
            messages=messages,
            images=list(self.images),
        )


def _short(args: dict[str, Any], limit: int = 80) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= limit else s[:limit] + "…"


class LoadToolsTool(Tool):
    """Carga el esquema completo de tools concretas en el contexto (RF-10)."""

    name = "tools.load"
    description = (
        "Carga los parámetros de las herramientas indicadas antes de usarlas "
        "(las que aparecen sin parámetros)."
    )
    parameters = {
        "type": "object",
        "required": ["names"],
        "properties": {"names": {"type": "array", "items": {"type": "string"}}},
    }
    risk_class = RiskClass.READ
    idempotent = True

    def __init__(self, context: ContextManager) -> None:
        self._context = context

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        names = args.get("names") or []
        if isinstance(names, str):
            names = [names]
        ok, unknown = self._context.load_tools([str(n) for n in names])
        if unknown and not ok:
            raise ToolError(f"herramientas desconocidas: {unknown}")
        msg = f"cargadas: {ok}" + (f"; desconocidas: {unknown}" if unknown else "")
        return ToolResult(msg + ". Sus parámetros ya aparecen en la lista de herramientas.")


class ReadRefTool(Tool):
    """Paginación de salidas truncadas (RF-CTX-03)."""

    name = "context.read_ref"
    description = "Lee un fragmento de una salida larga previamente truncada (por su ref)."
    parameters = {
        "type": "object",
        "required": ["ref"],
        "properties": {
            "ref": {"type": "string"},
            "offset": {"type": "integer", "default": 0},
            "length": {"type": "integer", "default": 3000},
        },
    }
    risk_class = RiskClass.READ
    idempotent = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ref = str(args.get("ref", ""))
        if not ref.startswith("sha256:") or not ref[7:].isalnum():
            raise ToolError("ref inválida")
        try:
            text = ctx.store.get_blob(ref)
        except FileNotFoundError as exc:
            raise ToolError(f"ref desconocida: {ref}") from exc
        offset = max(0, int(args.get("offset") or 0))
        # Por debajo de observation_max_chars para que la página no vuelva a truncarse.
        length = min(max(1, int(args.get("length") or 3000)), 3000)
        chunk = text[offset : offset + length]
        return ToolResult(f"[{offset}:{offset + len(chunk)} de {len(text)}]\n{chunk}")
