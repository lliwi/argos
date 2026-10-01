"""Piezas en proceso del núcleo persistente: bus de eventos, aprobaciones y sesiones (RF-04).

- `EventBus` retransmite cada evento de auditoría (ya redactado) a los suscriptores de la sesión
  raíz a la que pertenece, incluidos los de sus subagentes (RF-OB-03).
- `ApprovalHub` entrega las solicitudes HITL a cualquier canal conectado y acepta la respuesta de
  cualquiera de ellos (RF-20). Sin respuesta a tiempo => timeout => la acción no se ejecuta
  (RF-GOV-05).
- `SessionManager` lanza sesiones como tareas en segundo plano con id conocido y las cancela.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from argos.audit.store import AuditStore
from argos.config import Config
from argos.core.session import SessionOptions, SessionRefused, run_session
from argos.governance.approval import ApprovalRequest, Approver, Decision, NoApprover
from argos.model.base import ModelProvider


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._root: dict[str, str] = {}  # sesión -> sesión raíz

    def root_of(self, session_id: str) -> str:
        return self._root.get(session_id, session_id)

    def publish(self, event: dict[str, Any]) -> None:
        sid = event.get("session_id", "")
        if event.get("type") == "session" and event.get("parent_session_id"):
            self._root[sid] = self.root_of(event["parent_session_id"])
        for key in {sid, self.root_of(sid), "*"}:
            for queue in self._subs.get(key, ()):
                queue.put_nowait(event)

    def subscribe(self, key: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(key, set()).add(queue)
        return queue

    def unsubscribe(self, key: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.get(key, set()).discard(queue)


@dataclass
class PendingApproval:
    id: str
    session_id: str
    request: ApprovalRequest
    origin_channel: str
    created_at: str
    future: asyncio.Future[tuple[Decision, str, str]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "action": self.request.action,
            "risk_class": self.request.risk_class,
            "details": self.request.details,
            "timeout_s": self.request.timeout_s,
            "origin_channel": self.origin_channel,
            "created_at": self.created_at,
        }


class ApprovalHub:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.pending: dict[str, PendingApproval] = {}

    def approver_for(self, session_id: str, origin_channel: str) -> Approver:
        return _SessionApprover(self, session_id, origin_channel)

    async def ask(
        self, session_id: str, origin: str, req: ApprovalRequest
    ) -> tuple[Decision, str, str]:
        loop = asyncio.get_running_loop()
        pending = PendingApproval(
            uuid.uuid4().hex[:12],
            session_id,
            req,
            origin,
            datetime.now(UTC).isoformat(),
            loop.create_future(),
        )
        self.pending[pending.id] = pending
        # Se publica en el flujo de la sesión (y su raíz) como evento de canal, no de auditoría.
        self.bus.publish(
            {"type": "approval_request", "session_id": session_id, **pending.as_dict()}
        )
        try:
            return await asyncio.wait_for(asyncio.shield(pending.future), req.timeout_s)
        except TimeoutError:
            return "timeout", "none", origin
        finally:
            self.pending.pop(pending.id, None)
            if not pending.future.done():
                pending.future.cancel()

    def decide(self, approval_id: str, decision: Decision, approver: str, channel: str) -> bool:
        pending = self.pending.get(approval_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result((decision, approver, channel))
        return True


class _SessionApprover:
    """Approver de una sesión: `name`/`channel` reflejan quién respondió (para la auditoría)."""

    def __init__(self, hub: ApprovalHub, session_id: str, origin: str) -> None:
        self._hub = hub
        self._session_id = session_id
        self.name = "pending"
        self.channel = origin

    async def request(self, req: ApprovalRequest) -> Decision:
        decision, self.name, self.channel = await self._hub.ask(self._session_id, self.channel, req)
        return decision


@dataclass
class RunningSession:
    session_id: str
    task: asyncio.Task[Any]
    channel: str
    profile: str
    started_at: str
    result: dict[str, Any] | None = None
    tags: dict[str, str] = field(default_factory=dict)


class SessionManager:
    def __init__(
        self,
        cfg: Config,
        store: AuditStore,
        provider_factory,
        hub: ApprovalHub,
        sandbox_factory=None,
    ) -> None:
        self.cfg = cfg
        self.sandbox_factory = sandbox_factory  # tests: sandbox en memoria
        self.store = store
        self.provider_factory = provider_factory
        self.hub = hub
        self.sessions: dict[str, RunningSession] = {}

    def start(
        self,
        opts: SessionOptions,
        *,
        interactive: bool = True,
        provider: ModelProvider | None = None,
        tags: dict[str, str] | None = None,
    ) -> str:
        """Lanza la sesión y devuelve su id sin esperar. `interactive=False` (tareas programadas
        o webhooks) usa un aprobador que siempre deniega: nadie está mirando (RF-GOV-05)."""
        if not self.cfg.allows_profile(opts.profile):
            raise SessionRefused(
                f"el perfil {opts.profile!r} no pertenece al segmento {self.cfg.segment!r}"
            )
        sid = opts.session_id or uuid.uuid4().hex
        opts.session_id = sid
        approver = self.hub.approver_for(sid, opts.channel) if interactive else NoApprover()
        coro = run_session(
            opts,
            self.cfg,
            provider or self.provider_factory(),
            store=self.store,
            approver=approver,
            sandbox_factory=self.sandbox_factory,
        )
        task = asyncio.create_task(coro, name=f"session-{sid[:12]}")
        entry = RunningSession(
            sid, task, opts.channel, opts.profile, datetime.now(UTC).isoformat(), tags=tags or {}
        )
        self.sessions[sid] = entry
        task.add_done_callback(lambda t, e=entry: self._finished(e, t))
        return sid

    def _finished(self, entry: RunningSession, task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            entry.result = {"status": "aborted", "message": "cancelada"}
        elif (exc := task.exception()) is not None:
            entry.result = {"status": "failed", "message": f"{type(exc).__name__}: {exc}"}
        else:
            r = task.result()
            entry.result = {
                "status": r.status,
                "message": r.message,
                "steps": r.steps,
                "tokens": r.tokens,
            }

    def running(self) -> list[RunningSession]:
        return [s for s in self.sessions.values() if not s.task.done()]

    def cancel(self, session_id: str) -> bool:
        entry = self.sessions.get(session_id)
        if entry is None or entry.task.done():
            return False
        entry.task.cancel()
        return True

    async def shutdown(self) -> None:
        for entry in self.running():
            entry.task.cancel()
        await asyncio.gather(*(e.task for e in self.sessions.values()), return_exceptions=True)
