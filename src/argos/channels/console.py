"""Consola de operador (canal TUI, RF-06) con integración opcional con Herdr (RNF-05).

Sigue en vivo todas las sesiones del núcleo persistente, muestra su actividad de forma compacta y
resuelve aprobaciones desde el teclado. Dentro de un pane de Herdr (`HERDR_ENV=1`), además:

- informa del estado del pane: `working` (sesiones en curso), `blocked` (aprobación pendiente) o
  `idle`, para que Herdr lo muestre en su barra como a cualquier agente;
- lanza una notificación con sonido cuando llega una aprobación.

Corre en el host, no en el contenedor del núcleo: el socket de Herdr controla terminales del
usuario y no debe estar al alcance de un núcleo que procesa datos no confiables (P2).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markup import escape

from argos.server.client import CoreClient

State = str  # idle | working | blocked


class HerdrReporter:
    """Publica el estado del pane en Herdr. Sin Herdr (o fuera de un pane) no hace nada."""

    def __init__(
        self,
        binary: str | None = None,
        env: dict[str, str] | None = None,
        runner: Callable[[list[str]], Any] | None = None,
    ) -> None:
        env = env if env is not None else dict(os.environ)
        self.binary = binary or shutil.which("herdr") or "herdr"
        self.pane = env.get("HERDR_PANE_ID")
        self.enabled = env.get("HERDR_ENV") == "1" and bool(self.pane)
        self._runner = runner or self._run
        self._seq = 0
        self._last: tuple[str, str] | None = None

    async def _run(self, args: list[str]) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await asyncio.wait_for(proc.wait(), 5)
        except (OSError, TimeoutError):
            pass  # Herdr no disponible: la consola sigue funcionando igual

    async def state(self, state: State, message: str) -> None:
        if not self.enabled or self._last == (state, message):
            return
        self._last = (state, message)
        self._seq += 1
        await self._runner(
            [
                self.binary,
                "pane",
                "report-agent",
                self.pane,
                "--source",
                "argos",
                "--agent",
                "argos",
                "--state",
                state,
                "--message",
                message,
                "--seq",
                str(self._seq),
            ]
        )

    async def notify(self, title: str, body: str) -> None:
        if self.enabled:
            await self._runner(
                [self.binary, "notification", "show", title, "--body", body, "--sound", "request"]
            )

    async def release(self) -> None:
        if self.enabled:
            self._seq += 1
            await self._runner(
                [
                    self.binary,
                    "pane",
                    "release-agent",
                    self.pane,
                    "--source",
                    "argos",
                    "--agent",
                    "argos",
                    "--seq",
                    str(self._seq),
                ]
            )


def describe(ev: dict[str, Any]) -> str | None:
    """Línea compacta para la consola; None = evento que no merece línea propia."""
    sid = ev.get("session_id", "")[:8]
    match ev.get("type"):
        case "session":
            where = f"{ev['agent_profile']}/{ev['channel']}"
            return f"[bold]▶ {sid}[/] {where}: {escape(ev['task'][:90])}"
        case "turn" if ev.get("purpose") == "decide":
            d = ev.get("decision", {})
            if d.get("type") == "final":
                return None
            return f"  {sid} → [cyan]{escape(str(d.get('tool')))}[/]"
        case "error_event":
            return f"  {sid} [red]{ev['kind']}[/]: {escape(ev['message'][:120])}"
        case "approval":
            who = f"{ev['approver']} vía {ev['channel']}"
            return f"  {sid} [yellow]aprobación {ev['decision']}[/] ({who})"
        case "session_end":
            color = "green" if ev["status"] == "completed" else "red"
            return f"[{color}]■ {sid} {ev['status']}[/]: {escape((ev.get('result') or '')[:140])}"
    return None


async def run_console(
    client: CoreClient,
    reporter: HerdrReporter,
    console: Console,
    ask: Callable[[str, float], Any] | None = None,
    poll_s: float = 2.0,
) -> None:
    """Bucle de la consola. `ask(prompt, timeout)` devuelve la respuesta o None (tests)."""
    approvals: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    seen: set[str] = set()

    async def default_ask(prompt: str, limit_s: float) -> str | None:
        if not sys.stdin.isatty():
            return None
        try:
            return await asyncio.wait_for(asyncio.to_thread(input, prompt), limit_s)
        except TimeoutError:
            return None

    ask = ask or default_ask

    async def follow() -> None:
        async for ev in client.events_all():
            if ev.get("type") == "approval_request":
                if ev["id"] not in seen:
                    seen.add(ev["id"])
                    await approvals.put(ev)
                    # Inmediato, sin esperar al sondeo: Herdr debe marcar el pane en cuanto
                    # alguien tenga que decidir.
                    await reporter.state("blocked", f"aprobación: {ev['action']}")
                    await reporter.notify(
                        "Argos: aprobación pendiente", f"{ev['action']} ({ev['risk_class']})"
                    )
                continue
            if line := describe(ev):
                console.print(line, highlight=False)

    async def publish_state() -> None:
        while True:
            try:
                h = await client.state()
                if h["pending_approvals"]:
                    await reporter.state("blocked", f"{h['pending_approvals']} aprobación(es)")
                elif h["running_sessions"]:
                    await reporter.state("working", f"{h['running_sessions']} sesión(es)")
                else:
                    await reporter.state("idle", "sin sesiones")
            except Exception:  # noqa: BLE001 — núcleo caído: se reintenta en el siguiente ciclo
                await reporter.state("idle", "núcleo no disponible")
            await asyncio.sleep(poll_s)

    async def answer() -> None:
        while True:
            req = await approvals.get()
            console.print(
                f"\n[bold yellow]APROBACIÓN {req['id']}[/] {escape(req['action'])} "
                f"(riesgo {req['risk_class']}, sesión {req['session_id'][:8]})"
            )
            console.print(req["details"], markup=False, highlight=False)
            reply = await ask("¿Aprobar? [s/N] ", req["timeout_s"])
            if reply is None:
                console.print("  sin respuesta: se aplicará el timeout (denegada)")
                continue
            decision = "approved" if reply.strip().lower() in ("s", "si", "sí", "y") else "denied"
            ok = await client.decide(
                req["id"], decision, os.environ.get("USER", "console"), "console"
            )
            console.print(f"  {decision}" + ("" if ok else " (ya estaba resuelta)"))

    tasks = [asyncio.create_task(t()) for t in (follow, publish_state, answer)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()
    finally:
        for t in tasks:
            t.cancel()
        await reporter.release()
