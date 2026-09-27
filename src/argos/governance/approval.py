"""Aprobación humana (HITL) con política fail-safe (RF-GOV-04/05, P6).

El núcleo pide aprobación a un `Approver`, que la entrega por el canal activo. Sin aprobador o
con timeout, la decisión es negativa: nunca se procede por defecto.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Literal, Protocol

Decision = Literal["approved", "denied", "timeout"]


@dataclass
class ApprovalRequest:
    action: str
    risk_class: str
    details: str
    timeout_s: int


class Approver(Protocol):
    name: str
    channel: str

    async def request(self, req: ApprovalRequest) -> Decision: ...


class NoApprover:
    """Sin humano disponible (p. ej. tarea programada): se deniega siempre (RF-GOV-05)."""

    name = "none"
    channel = "none"

    async def request(self, req: ApprovalRequest) -> Decision:
        return "timeout"


class ScriptedApprover:
    """Respuestas predefinidas, para tests y evaluación."""

    name = "scripted"
    channel = "eval"

    def __init__(self, answers: list[Decision]) -> None:
        self.answers = list(answers)
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest) -> Decision:
        self.requests.append(req)
        return self.answers.pop(0) if self.answers else "timeout"


class CliApprover:
    """Pregunta en la terminal; sin respuesta en `timeout_s` => timeout."""

    name = "cli-user"
    channel = "cli"

    def __init__(self, console=None) -> None:
        self.console = console

    async def request(self, req: ApprovalRequest) -> Decision:
        if not sys.stdin.isatty():
            return "timeout"
        msg = (f"\n[APROBACIÓN] {req.action} (riesgo: {req.risk_class})\n{req.details}\n"
               f"¿Aprobar? [s/N] (timeout {req.timeout_s}s): ")
        if self.console:
            self.console.print(msg, style="bold yellow", end="")
        else:
            print(msg, end="", flush=True)
        try:
            answer = await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline), req.timeout_s)
        except TimeoutError:
            return "timeout"
        return "approved" if answer.strip().lower() in ("s", "si", "sí", "y", "yes") else "denied"
