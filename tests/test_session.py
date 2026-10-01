"""Tests de integración del bucle y la sesión con FakeProvider + FakeSandbox (sin red/Docker)."""

from __future__ import annotations

import asyncio

import pytest

from argos.audit.events import ErrorEvent, ErrorKind
from argos.audit.review import diff_sessions, replay_lines, summarize
from argos.core.session import SessionOptions, SessionRefused, run_session
from argos.governance.approval import ScriptedApprover
from argos.governance.killswitch import KillSwitch
from argos.model.fake import FakeProvider
from argos.sandbox.docker_sandbox import EgressDecision, ExecResult


def call(tool, **args):
    return {"type": "tool_call", "tool": tool, "args": args}


def final(msg="hecho"):
    return {"type": "final", "message": msg}


async def run(cfg, store, script, sandbox, task="tarea", approver=None, **opts):
    return await run_session(
        SessionOptions(task=task, **opts),
        cfg,
        FakeProvider(script),
        store=store,
        approver=approver,
        sandbox_factory=lambda: sandbox,
    )


def kinds(store, sid):
    return [e.kind for e in store.events(sid, ["error_event"]) if isinstance(e, ErrorEvent)]


async def test_multistep_task_is_fully_audited(cfg, store, fake_sandbox):
    """CA-1, CA-3, CA-4, CA-7: tool + shell, auditado, reproducible, con versiones."""
    script = [
        call("shell.exec", command="pip install pandas"),
        call("workspace.write_file", path="out/media.txt", content="5.5"),
        call("workspace.read_file", path="out/media.txt"),
        final("media 5.5"),
    ]
    res = await run(cfg, store, script, fake_sandbox)
    assert res.status == "completed" and res.steps == 4
    assert (res.workspace / "out/media.txt").read_text() == "5.5"
    types = [e.type for e in store.events(res.session_id)]
    for t in (
        "session",
        "turn",
        "shell_exec",
        "package_install",
        "tool_call",
        "file_event",
        "session_end",
    ):
        assert t in types, t
    s = summarize(store, res.session_id)
    assert s.tokens > 0 and s.steps == 4
    assert s.versions["prompt_version"] and s.versions["config_hash"]
    assert "shell.exec" in s.versions["tools"] and s.versions["harness_commit"]
    blocks = replay_lines(store, res.session_id)
    # bloque 0 = cabecera de sesión; luego un bloque por turno
    assert len(blocks) == 5 and any("pip install pandas" in line for line in blocks[1])
    assert fake_sandbox.destroyed


async def test_tool_failure_does_not_kill_session(cfg, store, fake_sandbox):
    """CA-5: el fallo queda registrado con su kind y la sesión sigue."""
    script = [
        call("workspace.read_file", path="in/no-existe.txt"),
        call("tool.inexistente"),
        final("recuperado"),
    ]
    res = await run(cfg, store, script, fake_sandbox)
    assert res.status == "completed"
    assert ErrorKind.TOOL_ERROR in kinds(store, res.session_id)
    assert ErrorKind.VALIDATION_ERROR in kinds(store, res.session_id)


async def test_invalid_model_output_is_fed_back(cfg, store, fake_sandbox):
    res = await run(cfg, store, ["no soy json", final()], fake_sandbox)
    assert res.status == "completed"
    assert ErrorKind.VALIDATION_ERROR in kinds(store, res.session_id)


async def test_model_error_fails_session_after_retries(cfg, store, fake_sandbox):
    script = [{"raise": "caído"}] * 3
    res = await run(cfg, store, script, fake_sandbox)
    assert res.status == "failed"
    assert kinds(store, res.session_id).count(ErrorKind.MODEL_ERROR) == 3


async def test_loop_detection(cfg, store, fake_sandbox):
    """RF-03."""
    res = await run(cfg, store, [call("workspace.list")] * 10, fake_sandbox)
    assert res.status == "aborted" and res.steps == cfg.loop.repeat_limit
    assert ErrorKind.LOOP_DETECTED in kinds(store, res.session_id)


async def test_budget_abort(cfg, store, fake_sandbox):
    """CA-9 (presupuesto): sin aprobador, la pausa termina en abort."""
    res = await run(
        cfg, store, [call("workspace.list")] * 5, fake_sandbox, session_budget_tokens=50
    )
    assert res.status == "aborted"
    actions = [e.action for e in store.events(res.session_id, ["budget_event"])]
    assert "pause" in actions
    assert ErrorKind.BUDGET_EXCEEDED in kinds(store, res.session_id)


async def test_kill_switch_refuses_new_sessions(cfg, store, fake_sandbox):
    KillSwitch(store).engage("prueba")
    with pytest.raises(SessionRefused):
        await run(cfg, store, [final()], fake_sandbox)
    KillSwitch(store).rearm()
    assert (await run(cfg, store, [final()], fake_sandbox)).status == "completed"


async def test_kill_switch_stops_running_session(cfg, store, fake_sandbox):
    """CA-9 (kill switch): un comando largo se corta al activar el kill switch."""
    fake_sandbox.delay_s = 30
    task = asyncio.create_task(
        run(cfg, store, [call("shell.exec", command="sleep 30")], fake_sandbox)
    )
    await asyncio.sleep(0.3)
    KillSwitch(store).engage("test")
    res = await asyncio.wait_for(task, timeout=5)
    KillSwitch(store).rearm()
    assert res.status == "killed"
    assert ErrorKind.KILLED in kinds(store, res.session_id)
    assert store.sessions()[0]["status"] == "killed"


async def test_destructive_action_requires_approval(cfg, store, fake_sandbox):
    """RF-GOV-04/05: sin aprobación no se ejecuta; con aprobación sí."""
    script = [call("shell.exec", command="rm -rf /home/agent/cache"), final()]
    res = await run(cfg, store, list(script), fake_sandbox)  # NoApprover => timeout
    assert fake_sandbox.commands == []
    approvals = store.events(res.session_id, ["approval"])
    assert approvals[0].decision == "timeout"
    assert ErrorKind.APPROVAL_TIMEOUT in kinds(store, res.session_id)

    approver = ScriptedApprover(["approved"])
    await run(cfg, store, list(script), fake_sandbox, approver=approver)
    assert fake_sandbox.commands == ["rm -rf /home/agent/cache"]
    assert approver.requests[0].risk_class == "destructive"


async def test_dry_run_records_without_executing(cfg, store, fake_sandbox):
    """RF-19."""
    script = [
        call("shell.exec", command="rm -rf /x"),
        call("workspace.write_file", path="a.txt", content="x"),
        final(),
    ]
    res = await run(cfg, store, script, fake_sandbox, dry_run=True)
    assert fake_sandbox.commands == [] and not (res.workspace / "out/a.txt").exists()
    execs = store.events(res.session_id, ["shell_exec"])
    assert execs[0].dry_run is True
    assert not store.events(res.session_id, ["approval"])  # en dry-run no hay nada que aprobar


async def test_profile_outside_segment_is_refused(cfg, store, fake_sandbox):
    """RF-SEC-02: el núcleo del segmento main no ejecuta perfiles de osint/pentest."""
    with pytest.raises(SessionRefused, match="ARGOS_SEGMENT=osint"):
        await run(cfg, store, [final()], fake_sandbox, profile="osint")


async def test_offensive_command_out_of_scope_is_rejected(root, fake_sandbox):
    """RF-SEC-06: pentest solo contra objetivos del scope."""
    from argos.audit.store import AuditStore
    from argos.config import load_config

    cfg = load_config(root, {"data_dir": str(root / "var"), "segment": "pentest"})
    store = AuditStore(cfg.data_path)
    script = [call("shell.exec", command="nmap -sV victima.example.com"), final()]
    res = await run(
        cfg,
        store,
        script,
        fake_sandbox,
        profile="pentest",
        dry_run=False,
        approver=ScriptedApprover(["approved"]),
    )
    assert fake_sandbox.commands == []
    call_ev = store.events(res.session_id, ["tool_call"])[0]
    assert call_ev.status == "error" and call_ev.error_kind == ErrorKind.VALIDATION_ERROR


async def test_egress_block_is_visible_in_trace(cfg, store, fake_sandbox):
    """CA-6."""
    fake_sandbox.responses["curl"] = ExecResult(56, "", "403 Forbidden", 10)
    fake_sandbox.blocked = [EgressDecision("evil.example", 443, "blocked", "no en allowlist")]
    res = await run(
        cfg,
        store,
        [call("shell.exec", command="curl https://evil.example"), final("bloqueado")],
        fake_sandbox,
    )
    errs = [
        e
        for e in store.events(res.session_id, ["error_event"])
        if e.kind == ErrorKind.EGRESS_BLOCKED
    ]
    assert errs and "evil.example" in errs[0].message
    assert res.status == "completed"


async def test_large_output_is_truncated_and_pageable(cfg, store, fake_sandbox):
    """RF-CTX-03."""
    big = "x" * 20_000
    fake_sandbox.responses["cat"] = ExecResult(0, big, "", 1)
    provider = FakeProvider([call("shell.exec", command="cat big"), final()])
    res = await run_session(
        SessionOptions(task="t"), cfg, provider, store=store, sandbox_factory=lambda: fake_sandbox
    )
    last_prompt = provider.requests[-1].render()
    assert len(last_prompt) < 12_000 and "context.read_ref" in last_prompt
    assert res.status == "completed"


async def test_mcp_reminders_roundtrip(cfg, store, fake_sandbox, tmp_path):
    """Tool MCP real (subproceso stdio) con metadata de riesgo desde anotaciones."""
    script = [call("reminders.add", text="comprar pan"), call("reminders.list"), final()]
    res = await run(cfg, store, script, fake_sandbox, state_dir=tmp_path / "state")
    calls = store.events(res.session_id, ["tool_call"])
    assert [c.tool for c in calls] == ["reminders.add", "reminders.list"]
    assert calls[1].result_preview.startswith("#1") and calls[1].idempotent
    assert calls[0].mcp_server == "reminders" and not calls[0].idempotent


async def test_diff_between_runs(cfg, store, fake_sandbox):
    """RF-OB-05."""
    a = await run(cfg, store, [call("workspace.list"), final()], fake_sandbox)
    b = await run(cfg, store, [final()], fake_sandbox)
    d = diff_sessions(store, a.session_id, b.session_id)
    assert d["metrics"]["steps"] == (2, 1)
    assert any(line.startswith("-workspace.list") for line in d["actions"])


async def test_routing_escalates_after_failures(cfg, store, fake_sandbox):
    """RF-CTX-05: tras N pasos fallidos seguidos, la siguiente decisión usa la ruta `hard`."""
    provider = FakeProvider(
        [
            call("workspace.read_file", path="in/a"),
            call("workspace.read_file", path="in/b"),
            call("workspace.list"),
            final(),
        ]
    )
    res = await run_session(
        SessionOptions(task="t"), cfg, provider, store=store, sandbox_factory=lambda: fake_sandbox
    )
    assert [r.name for r in provider.routes] == ["decide", "decide", "hard", "decide"]
    turns = store.events(res.session_id, ["turn"])
    assert turns[2].route == "hard" and turns[2].model == "fake/hard"


async def test_large_output_is_summarized_with_internal_route(cfg, store, fake_sandbox):
    """RF-CTX-03: salida grande => resumen con ruta `internal` + ref; no cuenta como paso."""
    fake_sandbox.responses["cat"] = ExecResult(0, "linea\n" * 5000, "", 1)
    provider = FakeProvider(
        [
            call("shell.exec", command="cat log"),
            {"type": "final", "message": "5000 líneas 'linea'"},
            final("ok"),
        ]
    )
    res = await run_session(
        SessionOptions(task="t"), cfg, provider, store=store, sandbox_factory=lambda: fake_sandbox
    )
    assert [r.name for r in provider.routes] == ["decide", "internal", "decide"]
    assert "5000 líneas" in provider.requests[-1].render()
    assert "context.read_ref ref=sha256:" in provider.requests[-1].render()
    internal = [t for t in store.events(res.session_id, ["turn"]) if t.purpose == "internal"]
    assert len(internal) == 1 and summarize(store, res.session_id).steps == 2


async def test_subagent_clean_context_and_tree(cfg, store, fake_sandbox):
    """RF-02, RF-OB-03: el hijo arranca con contexto limpio, comparte workspace y su coste
    cuenta para el padre."""
    # El FakeProvider es compartido: el guion se consume en orden padre → hijo → padre.
    provider = FakeProvider(
        [
            call("agent.delegate", task="escribe hijo.txt"),
            call("workspace.write_file", path="hijo.txt", content="del hijo"),  # hijo
            final("hijo.txt escrito"),  # hijo
            call("workspace.read_file", path="out/hijo.txt"),
            final("padre ok"),
        ]
    )
    res = await run_session(
        SessionOptions(task="tarea padre"),
        cfg,
        provider,
        store=store,
        sandbox_factory=lambda: fake_sandbox,
    )
    assert res.status == "completed"
    children = store.children(res.session_id)
    assert len(children) == 1
    child_prompt = provider.requests[1].render()
    assert "escribe hijo.txt" in child_prompt and "tarea padre" not in child_prompt
    assert "- agent.delegate:" not in child_prompt  # max_depth=1: sin la tool
    call_ev = [
        e for e in store.events(res.session_id, ["tool_call"]) if e.tool == "agent.delegate"
    ][0]
    assert "hijo.txt escrito" in call_ev.result_preview
    child_tokens = summarize(store, children[0], include_children=False).tokens
    assert res.tokens > child_tokens > 0
    assert (res.workspace / "out/hijo.txt").read_text() == "del hijo"


async def test_scratchpad_emits_plan_events(cfg, store, fake_sandbox):
    """RF-CTX-07."""
    script = [
        call("scratchpad.write", section="plan", content="1. a\n2. b"),
        call("scratchpad.write", section="plan", content="1. a (hecho)\n2. b"),
        call("scratchpad.read"),
        final(),
    ]
    res = await run(cfg, store, script, fake_sandbox)
    plans = store.events(res.session_id, ["plan_event"])
    assert [p.plan_type for p in plans] == ["create", "revise"]
    assert "(hecho)" in store.events(res.session_id, ["tool_call"])[-1].result_preview


async def test_lazy_tool_loading(root, store, fake_sandbox):
    """RF-10: por encima del umbral, los esquemas se cargan con tools.load."""
    from argos.config import load_config

    cfg = load_config(root, {"data_dir": str(root / "var"), "loop.lazy_tools_over": 3})
    provider = FakeProvider([call("tools.load", names=["workspace.write_file"]), final()])
    await run_session(
        SessionOptions(task="t"), cfg, provider, store=store, sandbox_factory=lambda: fake_sandbox
    )
    before, after = provider.requests[0].render(), provider.requests[1].render()
    assert "parámetros no cargados" in before
    write_line = next(
        i for i, line in enumerate(after.splitlines()) if line.startswith("- workspace.write_file")
    )
    assert "parámetros:" in after.splitlines()[write_line + 1]
    assert len(before) < len(after)


async def test_secrets_never_reach_model_context(cfg, store, fake_sandbox):
    """RNF-06: aunque el agente imprima un secreto, no llega al contexto ni a la auditoría."""
    canary = "canary-Secret-9f8e7d6c5b"
    store.redactor.register_secret(canary)
    fake_sandbox.responses["echo"] = ExecResult(0, f"token={canary}\n", "", 1)
    provider = FakeProvider([call("shell.exec", command="echo $TOKEN"), final()])
    res = await run_session(
        SessionOptions(task="t"), cfg, provider, store=store, sandbox_factory=lambda: fake_sandbox
    )
    assert canary not in provider.requests[-1].render()
    assert "[REDACTED:" in provider.requests[-1].render()
    jsonl = (store.jsonl_dir / f"{res.session_id}.jsonl").read_text()
    assert canary not in jsonl


async def test_orchestrator_delegates_to_specialist_profile(root, store, fake_sandbox):
    """El orquestador enruta a un perfil especialista; el subagente corre con ESE perfil."""
    from argos.config import load_config

    cfg = load_config(root, {"data_dir": str(root / "var")})
    # guion compartido: padre (orchestrator) delega a infra; hijo (infra) usa infra.inventory.
    provider = FakeProvider(
        [
            call("agent.delegate", profile="infra", task="dime qué infra hay configurada"),
            call("infra.inventory"),  # lo ejecuta el subagente infra
            final("infra: portainer configurado"),  # final del subagente
            final("Te lo resume el especialista de infra."),  # final del orquestador
        ]
    )
    res = await run_session(
        SessionOptions(task="qué tengo en casa", profile="orchestrator"),
        cfg,
        provider,
        store=store,
        sandbox_factory=lambda: fake_sandbox,
    )
    assert res.status == "completed"
    children = store.children(res.session_id)
    assert len(children) == 1
    child_start = store.events(children[0], ["session"])[0]
    assert child_start.agent_profile == "infra"  # el subagente corre como infra
    # el subagente pudo usar una tool de infra que el orquestador no tiene
    assert any(c.tool == "infra.inventory" for c in store.events(children[0], ["tool_call"]))
    assert not any(c.tool == "infra.inventory" for c in store.events(res.session_id, ["tool_call"]))


async def test_orchestrator_cannot_delegate_to_unlisted_profile(root, store, fake_sandbox):
    """RF-SEC-02: no se puede delegar a un perfil fuera de la lista (p. ej. de otro segmento)."""
    from argos.config import load_config

    cfg = load_config(root, {"data_dir": str(root / "var")})
    provider = FakeProvider(
        [call("agent.delegate", profile="pentest", task="escanea algo"), final("no pude")]
    )
    res = await run_session(
        SessionOptions(task="x", profile="orchestrator"),
        cfg,
        provider,
        store=store,
        sandbox_factory=lambda: fake_sandbox,
    )
    dele = store.events(res.session_id, ["tool_call"])[0]
    assert dele.status == "error" and dele.error_kind == ErrorKind.VALIDATION_ERROR
    assert store.children(res.session_id) == []  # no se creó ningún subagente
