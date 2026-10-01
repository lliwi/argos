"""Concurrencia por perfil y global (RF-GOV-03)."""

from __future__ import annotations

import asyncio
import json

import pytest

from argos.config import load_config
from argos.core import session as session_mod
from argos.core.session import SessionOptions, SessionRefused, run_session
from argos.model.fake import FakeProvider


def call(tool, **args):
    return {"type": "tool_call", "tool": tool, "args": args}


def final(msg="hecho"):
    return {"type": "final", "message": msg}


class GatedProvider(FakeProvider):
    """Se queda "pensando" en su primera respuesta hasta que se abre `gate`."""

    def __init__(self, script, gate: asyncio.Event):
        super().__init__(script)
        self.gate = gate
        self.entered = asyncio.Event()

    async def complete(self, request, route=None):
        self.entered.set()
        await self.gate.wait()
        return await super().complete(request, route)


@pytest.fixture(autouse=True)
def _fresh_slots():
    session_mod._SLOTS.clear()  # los semáforos se atan al bucle de cada test
    yield
    session_mod._SLOTS.clear()


def _cfg(root, wait_s=0):
    return load_config(
        root,
        {"data_dir": str(root / "var"), "model.provider": "fake", "concurrency.wait_s": wait_s},
    )


def _run(cfg, store, provider, sandbox, profile, task="tarea"):
    return run_session(
        SessionOptions(task=task, profile=profile),
        cfg,
        provider,
        store=store,
        sandbox_factory=lambda: sandbox,
    )


async def _hold(cfg, store, sandbox, profile):
    """Lanza una sesión que ocupa su hueco hasta que se abra el gate devuelto."""
    gate = asyncio.Event()
    provider = GatedProvider([final("primera")], gate)
    task = asyncio.create_task(_run(cfg, store, provider, sandbox, profile))
    await asyncio.wait_for(provider.entered.wait(), 30)
    return gate, task


def test_profile_limits_from_config(root):
    cfg = _cfg(root)
    assert cfg.profile("infra").max_concurrent == 1
    assert cfg.profile("pentest").max_concurrent == 1
    assert cfg.profile("personal").max_concurrent == 2
    assert cfg.profile("osint").max_concurrent == 2
    assert cfg.profile("orchestrator").max_concurrent is None
    assert cfg.concurrency.wait_s == 0 and _cfg(root, 60).concurrency.wait_s == 60


async def test_profile_slot_full_refuses_and_logs(root, store, fake_sandbox):
    cfg = _cfg(root, wait_s=0)
    cfg.profiles["orchestrator"].max_concurrent = 1
    gate, first = await _hold(cfg, store, fake_sandbox, "orchestrator")
    with pytest.raises(SessionRefused, match="perfil orchestrator, 1"):
        await _run(cfg, store, FakeProvider([final()]), fake_sandbox, "orchestrator")
    gate.set()
    assert (await first).status == "completed"
    log = [
        json.loads(line) for line in (cfg.data_path / "concurrency.jsonl").read_text().splitlines()
    ]
    assert log[-1]["action"] == "refused" and log[-1]["profile"] == "orchestrator"
    # Liberado el hueco, entra la siguiente.
    res = await _run(cfg, store, FakeProvider([final()]), fake_sandbox, "orchestrator")
    assert res.status == "completed"


async def test_waits_for_slot_instead_of_failing(root, store, fake_sandbox):
    cfg = _cfg(root, wait_s=20)
    cfg.profiles["orchestrator"].max_concurrent = 1
    gate, first = await _hold(cfg, store, fake_sandbox, "orchestrator")
    second = asyncio.create_task(
        _run(cfg, store, FakeProvider([final("segunda")]), fake_sandbox, "orchestrator")
    )
    await asyncio.sleep(0.3)
    assert not second.done()  # en cola, no rechazada
    gate.set()
    assert (await first).status == "completed"
    assert (await asyncio.wait_for(second, 20)).message == "segunda"


async def test_global_limit_counts_root_sessions(root, store, fake_sandbox):
    cfg = _cfg(root, wait_s=0)
    cfg.concurrency.max_sessions = 1
    gate, first = await _hold(cfg, store, fake_sandbox, "orchestrator")
    with pytest.raises(SessionRefused, match="global, 1"):
        await _run(cfg, store, FakeProvider([final()]), fake_sandbox, "orchestrator")
    gate.set()
    await first


async def test_delegation_counts_for_target_profile(root, store, fake_sandbox):
    """Un subagente delegado a infra ocupa el hueco de infra: si está lleno, el orquestador
    recibe un rechazo claro y sigue (no se cae)."""
    cfg = _cfg(root, wait_s=0)
    gate, held = await _hold(cfg, store, fake_sandbox, "infra")
    provider = FakeProvider(
        [
            call("agent.delegate", profile="infra", task="cuántos contenedores hay"),
            final("infra está ocupado, lo intento luego"),
        ]
    )
    res = await _run(cfg, store, provider, fake_sandbox, "orchestrator")
    assert res.status == "completed"
    tc = next(c for c in store.events(res.session_id, ["tool_call"]) if c.tool == "agent.delegate")
    assert tc.status != "ok"
    assert store.children(res.session_id) == []  # el subagente no llegó a arrancar
    gate.set()
    await held


async def test_same_profile_subagent_does_not_deadlock(root, store, fake_sandbox):
    """infra (máx. 1) delegándose a sí mismo trabaja dentro de su propio hueco."""
    cfg = _cfg(root, wait_s=0)
    provider = FakeProvider(
        [
            call("agent.delegate", task="revisa el inventario"),
            call("infra.inventory"),
            final("inventario revisado"),
            final("listo"),
        ]
    )
    res = await _run(cfg, store, provider, fake_sandbox, "infra")
    assert res.status == "completed"
    assert len(store.children(res.session_id)) == 1
