"""Revisión de auditoría: replay (RF-OB-04), diff (RF-OB-05), coste (CA-4) y métricas (RF-OB-06).

Todo se reconstruye exclusivamente a partir del store de auditoría.
"""

from __future__ import annotations

import difflib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from rich.markup import escape

from argos.audit.events import (
    Approval,
    BudgetEvent,
    ErrorEvent,
    EvalRun,
    Event,
    FileEvent,
    MemoryEvent,
    PackageInstall,
    SessionEnded,
    SessionStarted,
    ShellExec,
    SkillActivation,
    ToolCall,
    Turn,
)
from argos.audit.store import AuditStore


def _clip(text: str | None, n: int = 160) -> str:
    text = (text or "").replace("\n", "⏎ ")
    return text if len(text) <= n else text[:n] + "…"


def render_event(ev: Event, store: AuditStore, full: bool = False) -> list[str]:
    """Una o varias líneas legibles por evento (marcado rich)."""
    t = ev.ts.strftime("%H:%M:%S")
    match ev:
        case SessionStarted():
            return [
                f"[bold]SESIÓN {ev.session_id}[/] perfil={ev.agent_profile} canal={ev.channel}"
                f" dry_run={ev.dry_run}",
                f"  tarea: {ev.task}",
                f"  modelo={ev.model} prompt={ev.prompt_version} config={ev.config_hash}"
                f" commit={ev.harness_commit}",
                f"  tools={ev.tools_versions}",
            ]
        case Turn():
            d = ev.decision
            if ev.purpose == "internal":
                return [
                    f"{t}   [dim]interno ({ev.route}, {ev.model}): {d.get('type')} de "
                    f"{d.get('tool')} ({ev.prompt_tokens}+{ev.completion_tokens} tok)[/]"
                ]
            what = (
                f"→ {d.get('tool')} {json.dumps(d.get('args', {}), ensure_ascii=False)}"
                if d.get("type") == "tool_call"
                else f"→ FINAL: {d.get('message')}"
            )
            return [
                f"{t} [cyan]turno {ev.seq}[/] {escape(f'[{ev.route or chr(45)}·{ev.model}]')} "
                f"({ev.prompt_tokens}+{ev.completion_tokens} tok, {ev.latency_ms} ms) "
                f"{_clip(what, 400)}"
            ]
        case ShellExec():
            lines = [
                f"{t}   [magenta]$ {ev.command}[/] → exit={ev.exit_code}"
                f" ({ev.duration_ms} ms){' [dry-run]' if ev.dry_run else ''}"
            ]
            if full:
                for label, ref in (("stdout", ev.stdout_ref), ("stderr", ev.stderr_ref)):
                    if ref:
                        lines.append(f"     {label}: {_clip(store.get_blob(ref), 2000)}")
            return lines
        case ToolCall():
            color = {"ok": "green", "error": "red", "denied": "yellow"}.get(ev.status, "white")
            return [
                f"{t}   [{color}]{ev.tool} {ev.status}[/] risk={ev.risk_class.value}"
                f" idem={ev.idempotent} intento={ev.attempt} ({ev.duration_ms} ms)"
                + (f" kind={ev.error_kind.value}" if ev.error_kind else "")
                + f"\n     ↳ {_clip(ev.result_preview, 300 if not full else 3000)}"
            ]
        case FileEvent():
            return [f"{t}   file {ev.op} {ev.path} ({ev.bytes} B)"]
        case PackageInstall():
            return [
                f"{t}   [blue]install {ev.manager}:{ev.package}"
                f"{'==' + ev.version if ev.version else ''} {ev.status}[/]"
            ]
        case ErrorEvent():
            return [f"{t}   [red]ERROR {ev.kind.value}[/]: {_clip(ev.message, 300)}"]
        case MemoryEvent():
            what = {
                "save": "guardada",
                "update": "corregida",
                "search": "búsqueda",
                "inject": "inyectada",
            }[ev.op]
            ids = ", ".join(ev.memory_ids) or "sin resultados"
            detail = f": {_clip(ev.detail, 120)}" if ev.detail else ""
            return [f"{t}   [blue]memoria {what}[/] ({ids}){detail}"]
        case SkillActivation():
            return [f"{t}   [blue]skill {ev.skill} {ev.version} activada[/]"]
        case Approval():
            return [
                f"{t}   [yellow]APROBACIÓN {ev.action} ({ev.risk_class.value}): "
                f"{ev.decision}[/] por {ev.approver} vía {ev.channel}"
            ]
        case BudgetEvent():
            return [
                f"{t}   [yellow]PRESUPUESTO {ev.scope}: {ev.action}[/]"
                f" {int(ev.spent)}/{int(ev.limit)} {ev.unit}"
            ]
        case EvalRun():
            return [f"{t}   EVAL {ev.suite}/{ev.task_id}: score={ev.score:.2f} passed={ev.passed}"]
        case SessionEnded():
            return [f"[bold]FIN[/] estado={ev.status} pasos={ev.steps}: {_clip(ev.result, 400)}"]
    return [f"{t}   {ev.type}"]


def replay_lines(store: AuditStore, session_id: str, full: bool = False) -> list[list[str]]:
    """Agrupa por turno: cada bloque es un paso reproducible."""
    blocks: list[list[str]] = [[]]
    for ev in store.events(session_id):
        if isinstance(ev, Turn) and ev.purpose == "decide" and blocks[-1]:
            blocks.append([])
        blocks[-1].extend(render_event(ev, store, full))
    return blocks


@dataclass
class SessionSummary:
    session_id: str
    status: str = "running"
    profile: str = ""
    model: str = ""
    versions: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    max_context_chars: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    shell_execs: int = 0
    installs: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    actions: list[str] = field(default_factory=list)
    per_turn: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def summarize(store: AuditStore, session_id: str, include_children: bool = True) -> SessionSummary:
    s = SessionSummary(session_id)
    events = store.events(session_id)
    if events:
        s.duration_s = (events[-1].ts - events[0].ts).total_seconds()
    for ev in events:
        match ev:
            case SessionStarted():
                s.profile, s.model = ev.agent_profile, ev.model
                s.versions = {
                    "model": ev.model,
                    "prompt_version": ev.prompt_version,
                    "config_hash": ev.config_hash,
                    "harness_commit": ev.harness_commit,
                    "tools": ev.tools_versions,
                    "skills": ev.skills_versions,
                }
            case Turn():
                s.steps += ev.purpose == "decide"  # los turnos internos no son pasos
                s.prompt_tokens += ev.prompt_tokens
                s.completion_tokens += ev.completion_tokens
                s.cached_tokens += ev.cached_tokens
                s.cost += ev.cost
                s.latency_ms += ev.latency_ms
                s.max_context_chars = max(s.max_context_chars, ev.context_chars)
                s.per_turn.append(
                    {
                        "seq": ev.seq,
                        "route": ev.route,
                        "purpose": ev.purpose,
                        "model": ev.model,
                        "prompt": ev.prompt_tokens,
                        "completion": ev.completion_tokens,
                        "cached": ev.cached_tokens,
                        "cost": ev.cost,
                        "context_chars": ev.context_chars,
                    }
                )
                d = ev.decision
                if ev.purpose == "decide":
                    s.actions.append(
                        f"{d.get('tool')}" if d.get("type") == "tool_call" else "final"
                    )
            case ToolCall():
                s.tool_calls += 1
                s.tool_errors += ev.status == "error"
            case ShellExec():
                s.shell_execs += 1
            case PackageInstall():
                s.installs += 1
            case ErrorEvent():
                s.errors[ev.kind.value] += 1
            case SessionEnded():
                s.status = ev.status
    if include_children:
        for child in store.children(session_id):
            c = summarize(store, child)
            s.prompt_tokens += c.prompt_tokens
            s.completion_tokens += c.completion_tokens
            s.cost += c.cost
    return s


def diff_sessions(store: AuditStore, a: str, b: str) -> dict[str, Any]:
    sa, sb = summarize(store, a), summarize(store, b)
    versions = {
        k: (sa.versions.get(k), sb.versions.get(k))
        for k in sorted(set(sa.versions) | set(sb.versions))
        if sa.versions.get(k) != sb.versions.get(k)
    }
    metrics = {
        k: (getattr(sa, k), getattr(sb, k))
        for k in (
            "status",
            "steps",
            "tokens",
            "cost",
            "tool_calls",
            "tool_errors",
            "shell_execs",
            "latency_ms",
            "max_context_chars",
        )
    }
    actions = list(difflib.unified_diff(sa.actions, sb.actions, a[:8], b[:8], lineterm="", n=1))
    errors = {
        k: (sa.errors.get(k, 0), sb.errors.get(k, 0))
        for k in sorted(set(sa.errors) | set(sb.errors))
    }
    return {"versions": versions, "metrics": metrics, "errors": errors, "actions": actions}


def aggregate_metrics(store: AuditStore) -> dict[str, Any]:
    """Métricas agregadas (RF-OB-06) sobre toda la auditoría."""
    by_profile: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0, "completed": 0, "tokens": 0, "cost": 0.0, "duration_s": 0.0}
    )
    for row in store.sessions(limit=100_000):
        s = summarize(store, row["id"], include_children=False)
        p = by_profile[row["profile"]]
        p["sessions"] += 1
        p["completed"] += s.status == "completed"
        p["tokens"] += s.tokens
        p["cost"] += s.cost
        p["duration_s"] += s.duration_s
    tools: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "retries": 0, "duration_ms": 0}
    )
    for ev in store.iter_all(["tool_call"]):
        assert isinstance(ev, ToolCall)
        t = tools[ev.tool]
        t["calls"] += 1
        t["errors"] += ev.status == "error"
        t["retries"] += max(0, ev.attempt - 1)
        t["duration_ms"] += ev.duration_ms
    errors = Counter(
        ev.kind.value for ev in store.iter_all(["error_event"]) if isinstance(ev, ErrorEvent)
    )
    return {"profiles": dict(by_profile), "tools": dict(tools), "errors": dict(errors)}
