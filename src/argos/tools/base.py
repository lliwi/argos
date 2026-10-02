"""Contrato de tool (RF-21): cada tool declara riesgo e idempotencia; eso gobierna reintentos
(RNF-04) y aprobaciones (§13)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from argos.audit.events import ErrorKind, Event, RiskClass

if TYPE_CHECKING:
    from argos.audit.store import AuditStore
    from argos.config import Profile
    from argos.sandbox.docker_sandbox import Sandbox


class ToolError(Exception):
    def __init__(self, message: str, kind: ErrorKind = ErrorKind.TOOL_ERROR):
        super().__init__(message)
        self.kind = kind


@dataclass
class ToolContext:
    session_id: str
    turn_id: str
    trace_id: str
    profile: Profile
    workspace: Path
    store: AuditStore
    dry_run: bool = False
    sandbox: Sandbox | None = None
    scope: Any = None  # argos.pentest.Scope efectivo (RF-LEG-01); None => el del perfil
    # Emite eventos hijos de la tool call en curso (parent_span_id ya fijado).
    emit: Callable[[Event], None] = field(default=lambda e: None)


@dataclass
class ToolResult:
    output: str
    ok: bool = True
    error_kind: ErrorKind | None = None
    data: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    name: str
    version: str = "0.1.0"
    description: str
    parameters: dict[str, Any]
    risk_class: RiskClass = RiskClass.READ
    idempotent: bool = True
    mcp_server: str | None = None

    def risk_for(self, args: dict[str, Any]) -> RiskClass:
        """Riesgo efectivo de una invocación concreta (p. ej. `rm -rf` eleva un shell)."""
        return self.risk_class

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    async def aclose(self) -> None:  # noqa: B027 — hook opcional
        pass
