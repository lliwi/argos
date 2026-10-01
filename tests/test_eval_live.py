"""Evaluación de eficacia real: verdad en vivo (checks `live`) y evaluación programada."""

from __future__ import annotations

import asyncio
import json

import pytest

from argos.eval.probes import Truth, commute_decision, judge


def test_judge_counts_names_numbers_and_choices():
    assert judge(Truth("count", 2), "Sí, hay 2 luces encendidas")[0]
    assert not judge(Truth("count", 2), "Hay 3 luces encendidas")[0]
    assert judge(Truth("count", 0), "No hay ninguna luz encendida")[0]
    assert judge(Truth("count", 3, extra={"all_ok": True}), "Sí, todos funcionan")[0]
    assert judge(Truth("names", ["jackett", "sonarr"]), "Parados: Jackett y sonarr")[0]
    ok, detail = judge(Truth("names", ["jackett", "sonarr"]), "Solo jackett está parado")
    assert not ok and "sonarr" in detail
    assert judge(Truth("names", []), "Ninguno, todos están en marcha")[0]
    assert judge(Truth("names", ["Revisión técnica"]), "- revision tecnica")[0]  # tildes
    used = Truth("number", 77.0, extra={"alternatives": [23.0]})
    assert judge(used, "Te queda un 23 % libre", 2)[0]
    assert judge(used, "Está al 76,5 % de uso", 2)[0]
    assert not judge(used, "Le queda la mitad (50 %)", 2)[0]
    assert judge(Truth("choice", ["coche"]), "🚗 Coche: lluvia en la vuelta\nIda…")[0]
    assert not judge(Truth("choice", ["coche"]), "🚲 Bici: mañana seco")[0]
    assert judge(Truth("choice", ["bici", "coche"]), "🚲 Bici")[0]  # ambiguo: ambas


def test_commute_rules():
    rows = [
        {"hour": "07:00", "temp_c": 12, "rain_mm": 0, "rain_prob": 10, "wind_kmh": 8},
        {"hour": "08:00", "temp_c": 13, "rain_mm": 0.5, "rain_prob": 60, "wind_kmh": 8},
        {"hour": "17:00", "temp_c": 18, "rain_mm": 0, "rain_prob": 0, "wind_kmh": 12},
    ]
    rules = {"rain_mm": 0.2, "rain_prob": 40, "wind_kmh": 30, "temp_c": 5}
    assert commute_decision(rows, {7, 17}, rules)[0] == "bici"
    decision, why = commute_decision(rows, {7, 8, 17, 18}, rules)
    assert decision == "coche" and "lluvia 08:00" in why
    cold = [{**rows[0], "temp_c": 3}]
    assert commute_decision(cold, {7}, rules) == ("coche", "frío 07:00")


def _task(tid="x01", **extra):
    return {
        "id": tid,
        "profile": "orchestrator",
        "prompt": "¿cuántas luces?",
        "fake_script": [{"type": "final", "message": "Hay 2 luces encendidas"}],
        "checks": [
            {"type": "status", "equals": "completed"},
            {"type": "live", "probe": "fake.lights"},
        ],
        **extra,
    }


async def test_live_check_scores_against_real_truth(cfg, store, fake_sandbox, monkeypatch):
    from argos.eval import probes
    from argos.eval.runner import run_task

    truth = {"n": 2}

    async def fake_probe(cfg, params):
        return Truth("count", truth["n"], "sonda simulada")

    monkeypatch.setitem(probes.PROBES, "fake.lights", fake_probe)
    ok = await run_task(_task(), cfg, store, "fake", None, "argos", None)
    assert ok.status == "passed" and ok.checks[1]["passed"]
    truth["n"] = 5  # la realidad cambió
    bad = await run_task(_task(), cfg, store, "fake", None, "argos", None)
    assert bad.status == "failed" and "esperado 5" in bad.checks[1]["detail"]


async def test_live_tasks_are_skipped_with_fake_provider(cfg, store):
    from argos.eval.runner import run_task

    out = await run_task(_task(requires=["live"]), cfg, store, "fake", None, "argos", None)
    assert out.status == "skipped" and "modelo real" in out.reason


async def test_checks_see_subagent_events(cfg, store, fake_sandbox):
    """Si el orquestador delega, la tool se llama en la sesión hija: el check debe verla."""
    from argos.eval.runner import run_task

    task = {
        "id": "x02",
        "profile": "orchestrator",
        "prompt": "tareas",
        "fake_script": [
            {
                "type": "tool_call",
                "tool": "agent.delegate",
                "args": {"profile": "personal", "task": "lista recordatorios"},
            },
            {"type": "tool_call", "tool": "reminders.list", "args": {}},
            {"type": "final", "message": "sin recordatorios"},
            {"type": "final", "message": "No tienes recordatorios"},
        ],
        "checks": [{"type": "tool_called", "tool": "reminders.list", "status": "ok"}],
    }
    out = await run_task(task, cfg, store, "fake", None, "argos", None)
    assert out.status == "passed", out.checks


def test_eval_schedule_validation(cfg):
    from argos.scheduler import ScheduleCfg, SchedulerCfg, load_scheduler_cfg

    with pytest.raises(ValueError):
        ScheduleCfg(name="e", cron="0 9 * * 0", kind="eval")  # sin suite
    with pytest.raises(ValueError):
        ScheduleCfg(name="a", cron="0 9 * * 0")  # agent sin task
    (cfg.root / "config" / "schedules.yaml").write_text(
        "schedules:\n  - {name: e, cron: '0 9 * * 0', kind: eval, suite: noexiste}\n"
    )
    with pytest.raises(ValueError, match="no existe la suite"):
        load_scheduler_cfg(cfg)
    assert (
        SchedulerCfg(
            schedules=[ScheduleCfg(name="e", cron="0 9 * * 0", kind="eval", suite="golden")]
        )
        .schedules[0]
        .profile
    )


async def test_scheduled_eval_publishes_report(root, fake_sandbox):
    from argos.audit.store import AuditStore
    from argos.config import load_config
    from argos.model.fake import FakeProvider
    from argos.scheduler import ScheduleCfg, SchedulerCfg
    from argos.server.app import Core

    suite = root / "evals" / "mini"
    suite.mkdir(parents=True)
    task = _task("m01")
    task["checks"] = [{"type": "output_contains", "pattern": "luces"}]
    (suite / "m01.yaml").write_text(json.dumps(task))
    cfg = load_config(root, {"data_dir": str(root / "var"), "model.provider": "fake"})
    store = AuditStore(cfg.data_path)
    sch = ScheduleCfg(
        name="evaluacion", title="Evaluación de Argos", cron="0 9 * * 0", kind="eval", suite="mini"
    )
    core = Core(cfg, store, lambda: FakeProvider([]), SchedulerCfg(schedules=[sch]))
    core.manager.sandbox_factory = lambda: fake_sandbox
    queue = core.bus.subscribe("*")
    for _ in range(2):  # la 2.ª se compara con la 1.ª
        ref = core.scheduler.fire(sch)
        assert ref and ref.startswith("eval-evaluacion-")
        assert core.scheduler.fire(sch) is None  # no se solapa consigo misma
        while (ev := await asyncio.wait_for(queue.get(), 20))["type"] != "eval_report":
            pass
    assert ev["title"] == "Evaluación de Argos" and ev["status"] == "completed"
    assert "1 tareas · éxito 100%" in ev["text"] and "antes: éxito 100%" in ev["text"]
    assert core.scheduler.status()[0]["kind"] == "eval"
