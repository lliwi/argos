"""Scheduler: tareas recurrentes (cron) y disparadas por webhook (§6.4).

- Cada ejecución es una sesión propia y auditada (RF-14, RF-15), canal `scheduler` o `webhook`.
- Nadie está mirando: las aprobaciones se deniegan (RF-GOV-05).
- Si la ejecución anterior de una tarea sigue en curso, la nueva se omite (sin solapes).
- El payload de un webhook es dato no confiable: entra como <untrusted> y el perfil destino no
  puede tener secretos potentes (P2, RF-SEC-03).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator

from argos.config import Config

log = logging.getLogger("argos.scheduler")


# --- cron (5 campos: minuto hora día-mes mes día-semana) -----------------------------------------

_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7)]
_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


class CronError(ValueError):
    pass


def _parse_field(expr: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise CronError(f"paso inválido en {part!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step_s else start
        if not (lo <= start <= end <= hi):
            raise CronError(f"{part!r} fuera de rango [{lo}-{hi}]")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class Cron:
    expr: str
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]
    dom_any: bool
    dow_any: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        text = _ALIASES.get(expr.strip(), expr.strip())
        parts = text.split()
        if len(parts) != 5:
            raise CronError(f"se esperan 5 campos: {expr!r}")
        try:
            sets = [_parse_field(p, lo, hi) for p, (_, lo, hi) in zip(parts, _FIELDS, strict=True)]
        except ValueError as exc:
            raise CronError(f"{expr!r}: {exc}") from exc
        dow = {d % 7 for d in sets[4]}  # 7 = domingo = 0
        return cls(
            expr,
            *(frozenset(s) for s in sets[:4]),
            frozenset(dow),
            parts[2] == "*",
            parts[4] == "*",
        )

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minute or dt.hour not in self.hour or dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = (dt.isoweekday() % 7) in self.dow
        # Semántica cron clásica: si ambos están restringidos, basta con uno (OR).
        if self.dom_any or self.dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


# --- configuración -------------------------------------------------------------------------------


class ScheduleCfg(BaseModel):
    name: str
    cron: str
    # agent = un encargo al agente (task); eval = corre una suite de evaluación (suite) y publica
    # un informe comparado con la corrida anterior (RF-EV-02/05 de forma continua).
    kind: Literal["agent", "eval"] = "agent"
    task: str = ""
    suite: str | None = None
    # Por defecto el orquestador: único punto de entrada, pide el trabajo al perfil adecuado.
    profile: str = "orchestrator"
    title: str | None = None  # nombre legible para los avisos (por defecto, `name`)
    notify: bool = True  # enviar el resultado al canal de avisos (Matrix)
    enabled: bool = True
    dry_run: bool | None = None
    budget_tokens: int | None = None

    @model_validator(mode="after")
    def _valid_cron(self) -> ScheduleCfg:
        Cron.parse(self.cron)
        if self.kind == "agent" and not self.task.strip():
            raise ValueError(f"tarea {self.name!r}: falta 'task'")
        if self.kind == "eval" and not self.suite:
            raise ValueError(f"tarea {self.name!r}: kind eval necesita 'suite'")
        return self


class HookCfg(BaseModel):
    name: str
    profile: str
    task_template: str  # con {payload}
    token_env: str  # variable de entorno con el token compartido
    enabled: bool = True
    max_payload_chars: int = 8000


class SchedulerCfg(BaseModel):
    timezone: str = "UTC"
    schedules: list[ScheduleCfg] = Field(default_factory=list)
    hooks: list[HookCfg] = Field(default_factory=list)


def load_scheduler_cfg(cfg: Config) -> SchedulerCfg:
    path = cfg.root / "config" / "schedules.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    sc = SchedulerCfg.model_validate(data or {})
    ZoneInfo(sc.timezone)  # valida la zona
    for item in [*sc.schedules, *sc.hooks]:
        cfg.profile(item.profile)
    for sch in sc.schedules:
        if sch.kind == "eval" and not (cfg.root / "evals" / str(sch.suite)).is_dir():
            raise ValueError(f"tarea {sch.name!r}: no existe la suite evals/{sch.suite}")
    for hook in sc.hooks:
        prof = cfg.profile(hook.profile)
        if prof.is_powerful:
            raise ValueError(
                f"hook {hook.name!r}: el perfil {prof.name!r} tiene secretos potentes;"
                " un payload externo no confiable no puede llegar ahí (P2)"
            )
    # Cada segmento tiene su daemon (ADR-0026): solo se queda con lo que puede ejecutar, para
    # que el daemon de osint no dispare (ni rechace en bucle) las tareas del orquestador.
    sc.schedules = [s for s in sc.schedules if cfg.allows_profile(s.profile)]
    sc.hooks = [h for h in sc.hooks if cfg.allows_profile(h.profile)]
    return sc


def render_hook_task(hook: HookCfg, payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    text = text[: hook.max_payload_chars]
    return hook.task_template.replace(
        "{payload}", f'<untrusted source="webhook:{hook.name}">\n{text}\n</untrusted>'
    )


# --- bucle ---------------------------------------------------------------------------------------


@dataclass
class ScheduleState:
    last_fired: str | None = None  # minuto (ISO) de la última ejecución
    last_session: str | None = None
    last_status: str | None = None
    skipped: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


class Scheduler:
    def __init__(self, cfg: Config, sched: SchedulerCfg, manager) -> None:
        self.cfg = cfg
        self.sched = sched
        self.manager = manager
        self.tz = ZoneInfo(sched.timezone)
        self.state = {s.name: ScheduleState() for s in sched.schedules}
        self.log_path: Path = cfg.data_path / "scheduler.jsonl"
        self.last_tick: str | None = None
        self._evals: dict[str, asyncio.Task[Any]] = {}

    def _log(self, **entry: Any) -> None:
        entry = {"ts": datetime.now(self.tz).isoformat(), **entry}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _busy(self, name: str) -> bool:
        return any(s.tags.get("schedule") == name for s in self.manager.running())

    def fire(self, schedule: ScheduleCfg, reason: str = "cron") -> str | None:
        from argos.core.session import SessionOptions, SessionRefused

        st = self.state[schedule.name]
        if schedule.kind == "eval":
            return self._fire_eval(schedule, reason)
        if self._busy(schedule.name):
            st.skipped += 1
            self._log(schedule=schedule.name, action="skip", reason="ejecución anterior en curso")
            return None
        try:
            sid = self.manager.start(
                SessionOptions(
                    task=schedule.task,
                    profile=schedule.profile,
                    channel="scheduler",
                    dry_run=schedule.dry_run,
                    session_budget_tokens=schedule.budget_tokens,
                ),
                interactive=False,
                tags={"schedule": schedule.name},
            )
        except SessionRefused as exc:
            st.skipped += 1
            self._log(schedule=schedule.name, action="skip", reason=str(exc))
            return None
        st.last_session = sid
        self._log(schedule=schedule.name, action="fire", reason=reason, session_id=sid)
        return sid

    def _fire_eval(self, schedule: ScheduleCfg, reason: str) -> str | None:
        st = self.state[schedule.name]
        running = self._evals.get(schedule.name)
        if running and not running.done():
            st.skipped += 1
            self._log(schedule=schedule.name, action="skip", reason="evaluación anterior en curso")
            return None
        run_ref = f"eval-{schedule.name}-{datetime.now(self.tz):%Y%m%dT%H%M}"
        st.last_session = run_ref
        st.last_status = "running"
        self._evals[schedule.name] = asyncio.create_task(self._run_eval(schedule, run_ref))
        self._log(schedule=schedule.name, action="fire", reason=reason, session_id=run_ref)
        return run_ref

    async def _run_eval(self, schedule: ScheduleCfg, run_ref: str) -> None:
        from argos.eval.runner import compare_to_baseline, eval_report, latest_run, run_suite

        st = self.state[schedule.name]
        provider = self.cfg.model.provider
        try:
            previous = latest_run(self.cfg, str(schedule.suite), provider)
            summary = await run_suite(
                self.cfg,
                self.manager.store,
                str(schedule.suite),
                provider,
                self.manager.provider_factory,
            )
            regressions = compare_to_baseline(summary, previous) if previous else []
            text = eval_report(summary, previous, regressions)
            st.last_status = "regression" if regressions else "completed"
        except Exception as exc:  # noqa: BLE001 — el informe dice qué falló; el núcleo sigue
            text = f"la evaluación falló: {type(exc).__name__}: {exc}"
            st.last_status = "failed"
        self._log(
            schedule=schedule.name, action="result", session_id=run_ref, status=st.last_status
        )
        self.manager.hub.bus.publish(
            {
                "type": "eval_report",
                "schedule": schedule.name,
                "title": schedule.title or schedule.name,
                "notify": schedule.notify,
                "status": st.last_status,
                "text": text,
                "run_ref": run_ref,
            }
        )

    def tick(self, now: datetime | None = None) -> list[str]:
        now = (now or datetime.now(self.tz)).astimezone(self.tz).replace(second=0, microsecond=0)
        self.last_tick = now.isoformat()
        fired = []
        for sch in self.sched.schedules:
            st = self.state[sch.name]
            if not sch.enabled or st.last_fired == now.isoformat():
                continue
            if Cron.parse(sch.cron).matches(now):
                st.last_fired = now.isoformat()
                if sid := self.fire(sch):
                    fired.append(sid)
        return fired

    async def run(self, interval_s: float = 15) -> None:
        log.info("scheduler: %d tareas, zona %s", len(self.sched.schedules), self.sched.timezone)
        while True:
            try:
                self.tick()
                self._refresh_status()
            except Exception:  # noqa: BLE001 — el scheduler no debe morir por una tarea
                log.exception("fallo en tick del scheduler")
            await asyncio.sleep(interval_s)

    def _refresh_status(self) -> None:
        for name, st in self.state.items():
            entry = self.manager.sessions.get(st.last_session or "")
            if entry and entry.result and st.last_status != entry.result["status"]:
                st.last_status = entry.result["status"]
                self._log(
                    schedule=name,
                    action="result",
                    session_id=st.last_session,
                    status=st.last_status,
                )

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "title": s.title or s.name,
                "notify": s.notify,
                "kind": s.kind,
                "suite": s.suite,
                "cron": s.cron,
                "profile": s.profile,
                "enabled": s.enabled,
                "last_fired": self.state[s.name].last_fired,
                "last_session": self.state[s.name].last_session,
                "last_status": self.state[s.name].last_status,
                "skipped": self.state[s.name].skipped,
            }
            for s in self.sched.schedules
        ]
