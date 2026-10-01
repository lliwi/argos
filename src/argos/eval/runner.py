"""Runner de evaluación (§11): ejecuta tareas doradas, puntúa y registra `eval_run`.

- Reproducible en local y en CI con `--provider fake` (RF-EV-02).
- Resultado por corrida en `var/evals/<run>.json`; comparación contra línea base y puerta de
  regresión (RF-EV-04/05).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from argos.audit.events import EvalRun
from argos.audit.review import summarize
from argos.audit.store import AuditStore
from argos.config import Config
from argos.core.session import SessionOptions, run_session
from argos.eval.checks import CheckResult, run_check
from argos.eval.probes import live_check
from argos.governance.approval import ScriptedApprover
from argos.model.base import ModelProvider
from argos.model.fake import FakeProvider


@dataclass
class TaskOutcome:
    task_id: str
    status: str  # passed | failed | skipped | error
    score: float = 0.0
    session_id: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def load_suite(root: Path, suite: str) -> list[dict[str, Any]]:
    directory = root / "evals" / suite
    if not directory.is_dir():
        raise FileNotFoundError(f"suite inexistente: {directory}")
    return [yaml.safe_load(p.read_text()) for p in sorted(directory.glob("*.yaml"))]


def docker_ready(image: str) -> str | None:
    """None si Docker y la imagen están disponibles; si no, el motivo."""
    if shutil.which("docker") is None:
        return "docker no instalado"
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if probe.returncode != 0:
        return f"imagen {image} no construida (docker compose build)"
    return None


def sandbox_ready(cfg: Config) -> str | None:
    """None si hay sandbox disponible para las tareas que lo necesitan; si no, el motivo."""
    if cfg.sandbox.backend == "broker":
        sock = cfg.broker_socket()
        return None if sock.exists() else f"broker no disponible ({sock})"
    return docker_ready(cfg.sandbox.image)


async def run_task(
    task: dict[str, Any],
    cfg: Config,
    store: AuditStore,
    provider_name: str,
    make_provider,
    suite: str,
    baseline_ref: str | None,
) -> TaskOutcome:
    tid = task["id"]
    if "docker" in task.get("requires", []) and (why := sandbox_ready(cfg)):
        return TaskOutcome(tid, "skipped", reason=why)
    if provider_name == "fake" and "live" in task.get("requires", []):
        return TaskOutcome(tid, "skipped", reason="usa servicios reales: solo con modelo real")
    if provider_name == "fake":
        if "fake_script" not in task:
            return TaskOutcome(tid, "skipped", reason="sin fake_script para provider fake")
        provider: ModelProvider = FakeProvider(task["fake_script"])
    else:
        provider = make_provider()

    state_dir = cfg.data_path / "eval-state" / uuid.uuid4().hex[:12]
    opts = SessionOptions(
        task=task["prompt"],
        profile=task.get("profile", "personal"),
        channel="eval",
        dry_run=task.get("dry_run"),
        allow_domains=task.get("allow_domains", []),
        session_budget_tokens=task.get("session_budget_tokens"),
        input_files=task.get("input_files", {}),
        state_dir=state_dir,
    )
    try:
        result = await run_session(
            opts, cfg, provider, store=store, approver=ScriptedApprover(task.get("approvals", []))
        )
    except Exception as exc:  # noqa: BLE001 — una tarea rota no detiene la suite
        return TaskOutcome(tid, "error", reason=f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)

    events = tree_events(store, result.session_id)
    checks = []
    for spec in task.get("checks", []):
        if spec["type"] == "live":
            # Verdad en vivo: se consulta el servicio real y se compara con la respuesta.
            ok, detail = await live_check(spec, result.message or "", cfg)
            checks.append(
                CheckResult("live", ok, f"{spec['probe']}: {detail}", spec.get("required", True))
            )
        else:
            checks.append(run_check(spec, result, events))
    scored = [c for c in checks if c.required] or checks
    score = sum(c.passed for c in scored) / len(scored) if scored else 0.0
    passed = all(c.passed for c in checks if c.required)
    s = summarize(store, result.session_id)
    metrics = {
        "steps": s.steps,
        "tokens": s.tokens,
        "cost": round(s.cost, 6),
        "tool_calls": s.tool_calls,
        "tool_errors": s.tool_errors,
        "latency_ms": s.latency_ms,
        "duration_s": round(s.duration_s, 2),
    }
    store.emit(
        EvalRun(
            session_id=result.session_id,
            trace_id=result.session_id,
            suite=suite,
            task_id=tid,
            score=score,
            passed=passed,
            baseline_ref=baseline_ref,
            checks=[c.as_dict() for c in checks],
            metrics=metrics,
        )
    )
    return TaskOutcome(
        tid,
        "passed" if passed else "failed",
        score,
        result.session_id,
        [c.as_dict() for c in checks],
        metrics,
    )


def tree_events(store: AuditStore, session_id: str) -> list:
    """Eventos de la sesión y de todos sus subagentes: si el orquestador delega, la llamada a la
    tool y la aprobación ocurren en la sesión hija y los checks deben verlas."""
    out, pending = [], [session_id]
    while pending:
        sid = pending.pop()
        out.extend(store.events(sid))
        pending.extend(store.children(sid))
    return out


def per_task(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Agrega las corridas de cada tarea (varias si --repeat): tasa de éxito y medias."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for t in summary["tasks"]:
        if t["status"] in ("passed", "failed"):
            groups.setdefault(t["task_id"], []).append(t)
    out = {}
    for tid, runs in groups.items():
        n = len(runs)
        out[tid] = {
            "runs": n,
            "pass_rate": round(sum(r["status"] == "passed" for r in runs) / n, 3),
            "mean_score": round(sum(r["score"] for r in runs) / n, 3),
            "mean_tokens": round(sum(r["metrics"].get("tokens", 0) for r in runs) / n),
            "mean_steps": round(sum(r["metrics"].get("steps", 0) for r in runs) / n, 2),
        }
    return out


async def run_suite(
    cfg: Config,
    store: AuditStore,
    suite: str,
    provider_name: str,
    make_provider,
    only: list[str] | None = None,
    baseline_ref: str | None = None,
    repeat: int = 1,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tasks = [t for t in load_suite(cfg.root, suite) if not only or t["id"] in only]
    outcomes: list[TaskOutcome] = []
    for run in range(max(1, repeat)):
        for t in tasks:
            outcome = await run_task(
                t, cfg, store, provider_name, make_provider, suite, baseline_ref
            )
            outcome.metrics.setdefault("run", run + 1)
            outcomes.append(outcome)
    ran = [o for o in outcomes if o.status in ("passed", "failed")]
    summary = {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "suite": suite,
        "provider": provider_name,
        "config_hash": cfg.config_hash(),
        "overrides": overrides or {},
        "repeat": max(1, repeat),
        "aggregate": {
            "tasks": len(tasks),
            "ran": len(ran),
            "success_rate": round(sum(o.status == "passed" for o in ran) / len(ran), 3)
            if ran
            else 0.0,
            "mean_score": round(sum(o.score for o in ran) / len(ran), 3) if ran else 0.0,
            "mean_tokens": round(sum(o.metrics["tokens"] for o in ran) / len(ran)) if ran else 0,
            "mean_steps": round(sum(o.metrics["steps"] for o in ran) / len(ran), 2) if ran else 0,
            "mean_cost": round(sum(o.metrics["cost"] for o in ran) / len(ran), 6) if ran else 0,
        },
        "tasks": [asdict(o) for o in outcomes],
    }
    summary["per_task"] = per_task(summary)
    out_dir = cfg.data_path / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{summary['run_id']}.json").write_text(json.dumps(summary, indent=2))
    return summary


def compare_to_baseline(
    current: dict[str, Any], baseline: dict[str, Any], token_tolerance: float = 0.2
) -> list[str]:
    """Puerta de regresión (RF-EV-05): lista de regresiones; vacía => se puede promover."""
    regressions = []
    cur, base = per_task(current), per_task(baseline)
    for tid, c in cur.items():
        b = base.get(tid)
        if not b:
            continue
        if c["pass_rate"] < b["pass_rate"]:
            regressions.append(
                f"{tid}: tasa de éxito {b['pass_rate']:.0%} → {c['pass_rate']:.0%}"
                + (" (pasaba y ahora falla)" if b["pass_rate"] == 1 else "")
            )
        elif c["mean_score"] < b["mean_score"]:
            regressions.append(f"{tid}: score {b['mean_score']:.2f} → {c['mean_score']:.2f}")
        bt, ct = b["mean_tokens"], c["mean_tokens"]
        if bt and ct > bt * (1 + token_tolerance):
            regressions.append(f"{tid}: tokens {bt} → {ct} (>{token_tolerance:.0%})")
    ca, ba = current["aggregate"], baseline["aggregate"]
    if ca["success_rate"] < ba["success_rate"]:
        regressions.append(f"success_rate {ba['success_rate']} → {ca['success_rate']}")
    return regressions


def compare_runs(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[str, dict, dict]]:
    """Filas (tarea, métricas A, métricas B) para comparar dos corridas (RF-EV-04)."""
    pa, pb = per_task(a), per_task(b)
    return [(tid, pa.get(tid, {}), pb.get(tid, {})) for tid in sorted(set(pa) | set(pb))]


def latest_run(cfg: Config, suite: str, provider: str) -> dict[str, Any] | None:
    """Última corrida guardada de la suite con ese proveedor (referencia para comparar)."""
    out_dir = cfg.data_path / "evals"
    for path in sorted(out_dir.glob("*.json"), reverse=True) if out_dir.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if data.get("suite") == suite and data.get("provider") == provider:
            return data
    return None


def eval_report(
    summary: dict[str, Any], previous: dict[str, Any] | None, regressions: list[str]
) -> str:
    """Informe breve (para el móvil): aciertos, coste, fallos con su motivo y cambios."""
    agg = summary["aggregate"]
    lines = [
        f"{'⚠️' if regressions else '✅'} {agg['ran']} tareas · éxito "
        f"{agg['success_rate']:.0%} · score {agg['mean_score']:.2f} · "
        f"{agg['mean_tokens']:,} tokens de media".replace(",", ".")
    ]
    if previous:
        pa = previous["aggregate"]
        lines.append(
            f"antes: éxito {pa['success_rate']:.0%} · {pa['mean_tokens']:,} tokens".replace(
                ",", "."
            )
        )
    for t in summary["tasks"]:
        if t["status"] == "passed":
            continue
        why = t.get("reason") or "; ".join(
            c["detail"][:140] for c in t.get("checks", []) if c["required"] and not c["passed"]
        )
        icon = {"failed": "❌", "error": "💥", "skipped": "⏭"}.get(t["status"], "?")
        lines.append(f"{icon} {t['task_id']}: {why}")
    if regressions:
        lines.append("Regresiones:")
        lines += [f"• {r}" for r in regressions]
    lines.append(f"corrida {summary['run_id']} · detalle: argos audit show <sesión>")
    return "\n".join(lines)
