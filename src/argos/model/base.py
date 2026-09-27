"""Interfaz de motor de modelo (ADR-0001).

Un proveedor recibe el contexto ya construido por el núcleo y devuelve **una** decisión.
El bucle, la ejecución de tools y la auditoría son responsabilidad de Argos, no del proveedor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, ValidationError


class ModelError(RuntimeError):
    """Fallo del motor (red, auth, formato irrecuperable). Se audita como `model_error`."""


class ModelAuthError(ModelError):
    """Credenciales del motor inválidas/caducadas: no es transitorio, no se reintenta."""


class DecisionParseError(ValueError):
    """La respuesta no respeta el contrato de decisión. Se audita como `validation_error`."""


class Decision(BaseModel):
    type: Literal["tool_call", "final"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] | None   # None => no cargada (carga diferida, RF-10)


@dataclass
class Message:
    role: Literal["user", "assistant", "observation"]
    content: str


@dataclass
class ModelRequest:
    system: str
    tools: list[ToolSpec]
    messages: list[Message]

    def render(self) -> str:
        """Serialización textual única para proveedores sin tool-calling nativo.

        El prefijo (system + tools) es estable entre turnos, para favorecer caché (RF-CTX-01).
        """
        tools = "\n".join(
            f"- {t.name}: {t.description}\n"
            + (f"  parámetros: {json.dumps(t.parameters, ensure_ascii=False)}"
               if t.parameters is not None else "  (parámetros no cargados: usa tools.load)")
            for t in self.tools)
        parts = [self.system.strip(), "", "## Herramientas disponibles", tools, "", "## Historial"]
        for m in self.messages:
            parts.append(f"### {m.role}\n{m.content}")
        parts += [
            "", "## Tu decisión",
            'Responde SOLO con JSON: {"type": "tool_call"|"final", "tool": "<nombre o vacío>", '
            '"args_json": "<objeto JSON serializado>", "message": "<texto>"}',
        ]
        return "\n".join(parts)


@dataclass(frozen=True)
class Route:
    """Ruta de modelo (RF-CTX-05): qué modelo y esfuerzo usar para un tipo de trabajo."""

    name: str
    model: str | None = None
    effort: str | None = None


@dataclass
class ModelResponse:
    decision: Decision
    usage: Usage
    latency_ms: int
    raw_text: str
    # Acciones que el motor intentó por su cuenta (contrato violado), p. ej. comandos de Codex.
    violations: list[str] = field(default_factory=list)
    model: str = ""   # modelo efectivamente usado


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest,
                       route: Route | None = None) -> ModelResponse: ...


# JSON Schema de la decisión para salidas estructuradas. Todos los campos son obligatorios y
# `args_json` es texto porque el modo estricto no admite objetos de forma libre.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "tool", "args_json", "message"],
    "properties": {
        "type": {"type": "string", "enum": ["tool_call", "final"]},
        "tool": {"type": "string"},
        "args_json": {"type": "string"},
        "message": {"type": "string"},
    },
}

_JSON_OBJ = re.compile(r"\{.*\}", re.S)


def parse_decision(text: str) -> Decision:
    """Parser tolerante: admite JSON rodeado de texto/fences y `args` u `args_json`."""
    match = _JSON_OBJ.search(text or "")
    if not match:
        raise DecisionParseError(f"no hay objeto JSON en la respuesta: {text[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"JSON inválido: {exc}") from exc
    args = data.pop("args_json", None)
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError as exc:
            raise DecisionParseError(f"args_json no es JSON válido: {exc}") from exc
    if args is not None:
        data["args"] = args
    if not data.get("tool"):
        data["tool"] = None
    try:
        decision = Decision.model_validate(data)
    except ValidationError as exc:
        raise DecisionParseError(str(exc)) from exc
    if not isinstance(decision.args, dict):
        raise DecisionParseError("args debe ser un objeto")
    if decision.type == "tool_call" and not decision.tool:
        raise DecisionParseError("tool_call sin nombre de herramienta")
    return decision


def estimate_tokens(text: str) -> int:
    """Estimación barata (~4 caracteres/token) cuando el motor no informa uso."""
    return max(1, len(text) // 4)
