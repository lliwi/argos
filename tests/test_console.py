"""Consola de operador + integración con Herdr (RF-06, RF-20, RNF-05)."""

from __future__ import annotations

import asyncio
import io

from rich.console import Console
from test_server import call, collect, final, running_core

from argos.channels.console import HerdrReporter, run_console


def test_reporter_disabled_outside_herdr():
    calls = []
    r = HerdrReporter(env={}, runner=calls.append)
    asyncio.run(r.state("working", "x"))
    asyncio.run(r.notify("t", "b"))
    assert not r.enabled and calls == []


async def test_console_approves_and_reports_to_herdr(cfg, store, fake_sandbox):
    calls: list[list[str]] = []

    async def runner(args):
        calls.append(args)

    reporter = HerdrReporter(binary="herdr", env={"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p2"},
                             runner=runner)
    asked: list[str] = []

    async def ask(prompt, limit_s):
        asked.append(prompt)
        return "s"

    out = Console(file=io.StringIO(), width=200)
    script = [call("shell.exec", command="rm -rf /home/agent/cache"), final("limpio")]
    async with running_core(cfg, store, [script], fake_sandbox) as (core, client):
        console_task = asyncio.create_task(
            run_console(client, reporter, out, ask=ask, poll_s=0.05))
        await asyncio.sleep(0.2)
        sid = await client.submit(task="limpia la caché", profile="personal")
        events = await asyncio.wait_for(collect(client, sid), 10)
        await asyncio.sleep(0.3)
        console_task.cancel()
        await asyncio.gather(console_task, return_exceptions=True)

    assert events[-1]["status"] == "completed" and asked
    assert fake_sandbox.commands == ["rm -rf /home/agent/cache"]
    appr = store.events(sid, ["approval"])[0]
    assert appr.decision == "approved" and appr.channel == "console"
    states = [a[a.index("--state") + 1] for a in calls if "report-agent" in a]
    assert "blocked" in states and states[-1] == "idle"
    assert all(a[3] == "w1:p2" for a in calls if "report-agent" in a)
    assert any(a[1:3] == ["notification", "show"] for a in calls)
    assert calls[-1][1:3] == ["pane", "release-agent"]
    text = out.file.getvalue()
    assert "APROBACIÓN" in text and "completed" in text
