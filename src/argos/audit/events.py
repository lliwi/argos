"""Modelo de eventos de auditoría (REQUISITOS §10.2).

Cada evento lleva identificadores estilo OpenTelemetry (`trace_id`, `span_id`, `parent_span_id`)
para poder exportarse a trazas más adelante sin cambiar el modelo (ADR-0003).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class ErrorKind(StrEnum):
    """Taxonomía cerrada de errores (RF-OB-12)."""

    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    SANDBOX_ERROR = "sandbox_error"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    INJECTION_SUSPECTED = "injection_suspected"
    LOOP_DETECTED = "loop_detected"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_TIMEOUT = "approval_timeout"
    VALIDATION_ERROR = "validation_error"
    # Añadido a la taxonomía: bloqueo de egress del sandbox (CA-6). Ver RF-EX-04.
    EGRESS_BLOCKED = "egress_blocked"
    KILLED = "killed"


class RiskClass(StrEnum):
    """Clase de riesgo declarada por cada tool (RF-21)."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    OFFENSIVE = "offensive"


class Event(BaseModel):
    """Base común. `type` discrimina el tipo de evento en JSONL/SQLite."""

    type: str
    id: str = Field(default_factory=new_id)
    ts: datetime = Field(default_factory=now)
    session_id: str
    trace_id: str | None = None
    span_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str | None = None


class SessionStarted(Event):
    type: Literal["session"] = "session"
    agent_profile: str
    channel: str
    parent_session_id: str | None = None
    task: str
    model: str
    prompt_version: str
    skills_versions: dict[str, str] = Field(default_factory=dict)
    tools_versions: dict[str, str] = Field(default_factory=dict)
    config_hash: str
    harness_commit: str
    budget: dict[str, Any] = Field(default_factory=dict)
    authorization_ref: str | None = None
    dry_run: bool = False


class SessionEnded(Event):
    type: Literal["session_end"] = "session_end"
    status: Literal["completed", "failed", "aborted", "killed"]
    result: str | None = None
    steps: int = 0


class Turn(Event):
    type: Literal["turn"] = "turn"
    seq: int
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    context_chars: int = 0
    decision: dict[str, Any] = Field(default_factory=dict)
    route: str | None = None                          # RF-CTX-05
    purpose: Literal["decide", "internal"] = "decide"


class ToolCall(Event):
    type: Literal["tool_call"] = "tool_call"
    turn_id: str
    tool: str
    tool_version: str
    mcp_server: str | None = None
    args: dict[str, Any]
    result_ref: str | None = None
    result_preview: str | None = None
    status: Literal["ok", "error", "denied", "dry_run"]
    error_kind: ErrorKind | None = None
    duration_ms: int = 0
    idempotent: bool
    risk_class: RiskClass
    attempt: int = 1


class ShellExec(Event):
    type: Literal["shell_exec"] = "shell_exec"
    turn_id: str
    command: str
    cwd: str
    exit_code: int | None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    duration_ms: int = 0
    sandbox_id: str | None = None
    dry_run: bool = False


class FileEvent(Event):
    type: Literal["file_event"] = "file_event"
    turn_id: str
    path: str
    op: Literal["read", "write", "create", "delete", "list"]
    bytes: int = 0
    hash: str | None = None


class PackageInstall(Event):
    type: Literal["package_install"] = "package_install"
    turn_id: str
    manager: Literal["apt", "pip", "npm", "yarn", "cargo"]
    package: str
    version: str | None = None
    status: Literal["ok", "error"]


class PlanEvent(Event):
    type: Literal["plan_event"] = "plan_event"
    turn_id: str | None = None
    plan_type: Literal["create", "revise"]
    summary: str


class SkillActivation(Event):
    """RF-SK-06: qué skill, en qué versión y en qué turno."""

    type: Literal["skill_activation"] = "skill_activation"
    turn_id: str
    skill: str
    version: str


class MemoryEvent(Event):
    """Qué memoria se guardó, se consultó o se inyectó en la sesión (RF-16/17, P8)."""

    type: Literal["memory_event"] = "memory_event"
    turn_id: str | None = None
    op: Literal["save", "update", "search", "inject"]
    memory_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class Approval(Event):
    type: Literal["approval"] = "approval"
    turn_id: str
    action: str
    risk_class: RiskClass
    decision: Literal["approved", "denied", "timeout"]
    approver: str | None = None
    channel: str
    decided_at: datetime = Field(default_factory=now)


class ErrorEvent(Event):
    type: Literal["error_event"] = "error_event"
    turn_id: str | None = None
    kind: ErrorKind
    message: str
    retry_of: str | None = None


class BudgetEvent(Event):
    type: Literal["budget_event"] = "budget_event"
    scope: Literal["session", "day", "profile"]
    limit: float
    spent: float
    unit: Literal["tokens", "cost"] = "tokens"
    action: Literal["warn", "pause", "abort"]


class Feedback(Event):
    type: Literal["feedback"] = "feedback"
    rating: int | None = None
    correction: str | None = None
    source: Literal["human", "auto"]


class EvalRun(Event):
    type: Literal["eval_run"] = "eval_run"
    suite: str
    task_id: str
    score: float
    passed: bool
    baseline_ref: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


EVENT_TYPES: dict[str, type[Event]] = {
    cls.model_fields["type"].default: cls
    for cls in (
        SessionStarted, SessionEnded, Turn, ToolCall, ShellExec, FileEvent, PackageInstall,
        PlanEvent, SkillActivation, MemoryEvent, Approval, ErrorEvent, BudgetEvent, Feedback,
        EvalRun,
    )
}


def parse_event(data: dict[str, Any]) -> Event:
    return EVENT_TYPES[data["type"]].model_validate(data)
