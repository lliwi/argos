"""Checks deterministas de tareas doradas (ADR-0006). Evalúan workspace + auditoría."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from argos.audit.events import ErrorEvent, Event, SessionEnded, ToolCall, Turn
from argos.core.session import SessionResult


@dataclass
class CheckResult:
    type: str
    passed: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "passed": self.passed, "detail": self.detail,
                "required": self.required}


def _matches(ev: Event, where: dict[str, Any]) -> bool:
    data = ev.model_dump(mode="json")
    return all(str(data.get(k)) == str(v) for k, v in where.items())


def run_check(spec: dict[str, Any], result: SessionResult, events: list[Event]) -> CheckResult:
    kind = spec["type"]
    required = spec.get("required", True)

    def res(ok: bool, detail: str) -> CheckResult:
        return CheckResult(kind, ok, detail, required)

    match kind:
        case "status":
            return res(result.status == spec["equals"], f"status={result.status}")
        case "file_exists":
            path = result.workspace / spec["path"]
            return res(path.is_file(), str(spec["path"]))
        case "file_contains":
            path = result.workspace / spec["path"]
            if not path.is_file():
                return res(False, f"{spec['path']} no existe")
            text = path.read_text(errors="replace")
            ok = re.search(spec["pattern"], text) is not None
            return res(ok, f"/{spec['pattern']}/ en {spec['path']}: {text[:80]!r}")
        case "output_contains":
            ok = re.search(spec["pattern"], result.message or "", re.I) is not None
            return res(ok, f"/{spec['pattern']}/ en respuesta final")
        case "event":
            hits = [e for e in events if e.type == spec["event"]
                    and _matches(e, spec.get("where", {}))]
            n = spec.get("min", 1)
            return res(len(hits) >= n, f"{len(hits)} evento(s) {spec['event']} {spec.get('where')}")
        case "error":
            hits = [e for e in events if isinstance(e, ErrorEvent) and e.kind == spec["kind"]]
            return res(bool(hits), f"{len(hits)} error(es) {spec['kind']}")
        case "no_error":
            hits = [e for e in events if isinstance(e, ErrorEvent)
                    and (spec.get("kind") is None or e.kind == spec["kind"])]
            return res(not hits, f"{len(hits)} error(es) {spec.get('kind', '(cualquiera)')}")
        case "tool_called":
            hits = [e for e in events if isinstance(e, ToolCall) and e.tool == spec["tool"]
                    and (spec.get("status") is None or e.status == spec["status"])]
            return res(bool(hits), f"{len(hits)} llamada(s) a {spec['tool']}")
        case "max_steps":
            steps = sum(isinstance(e, Turn) and e.purpose == "decide" for e in events)
            return res(steps <= spec["value"], f"{steps} ≤ {spec['value']}")
        case "max_tokens":
            tokens = sum(e.prompt_tokens + e.completion_tokens for e in events
                         if isinstance(e, Turn))
            return res(tokens <= spec["value"], f"{tokens} ≤ {spec['value']}")
        case "session_ended":
            return res(any(isinstance(e, SessionEnded) for e in events), "session_end presente")
        case "llm_judge":
            return CheckResult(kind, False, "llm_judge no implementado (ADR-0006)", False)
    raise ValueError(f"tipo de check desconocido: {kind}")
