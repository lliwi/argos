"""API interna del núcleo persistente (RF-04) y receptor de webhooks.

- API completa sobre socket Unix (`<datos del segmento>/argos.sock`, modo 600): la autenticación
  es el permiso del fichero. Solo procesos del propio usuario/contenedor pueden hablar con ella.
- Webhooks en TCP (por defecto 127.0.0.1): solo `POST /hooks/<nombre>` con token compartido.
- Progreso en vivo por Server-Sent Events: los mismos eventos de auditoría, ya redactados.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import shutil
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from argos import __version__
from argos.audit.store import AuditStore
from argos.config import Config
from argos.core.session import SessionOptions, SessionRefused
from argos.governance.killswitch import KillSwitch
from argos.scheduler import Scheduler, SchedulerCfg, render_hook_task
from argos.server.hub import ApprovalHub, EventBus, SessionManager
from argos.state import StateStore

TERMINAL = {"completed", "failed", "aborted", "killed"}


class Core:
    """Estado del núcleo persistente, compartido por la API y los webhooks."""

    def __init__(self, cfg: Config, store: AuditStore, provider_factory,
                 sched_cfg: SchedulerCfg) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = EventBus()
        store.listeners.append(self.bus.publish)
        self.hub = ApprovalHub(self.bus)
        self.manager = SessionManager(cfg, store, provider_factory, self.hub)
        self.scheduler = Scheduler(cfg, sched_cfg, self.manager)
        self.kill = KillSwitch(store)
        self.state = StateStore(cfg.data_path / "state.db")
        self.started = time.time()

    # --- salud (RNF-11) --------------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        try:
            self.store.db.execute("SELECT 1").fetchone()
            checks["audit_store"] = "ok"
        except Exception as exc:  # noqa: BLE001
            checks["audit_store"] = f"error: {exc}"
        checks["sandbox"] = (await _broker_ok(self.cfg.broker_socket())
                             if self.cfg.sandbox.backend == "broker" else await _docker_ok())
        checks["egress_proxy"] = await asyncio.to_thread(_tcp_ok, self.cfg.segment_proxy())
        checks["model_engine"] = ("ok" if self.cfg.model.provider != "codex"
                                  or shutil.which("codex") else "error: codex no encontrado")
        checks["scheduler"] = {"last_tick": self.scheduler.last_tick}
        ok = all(v == "ok" for k, v in checks.items() if k != "scheduler"
                 and not str(v).startswith("unknown"))
        return {"status": "ok" if ok else "degraded", "version": __version__,
                "segment": self.cfg.segment, "uptime_s": int(time.time() - self.started),
                "kill_switch": self.kill.active(), "running_sessions": len(self.manager.running()),
                "pending_approvals": len(self.hub.pending), "checks": checks}


async def _docker_ok() -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "info", "--format", "{{.ServerVersion}}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return "ok" if await asyncio.wait_for(proc.wait(), 5) == 0 else "error: docker info"
    except (FileNotFoundError, TimeoutError):
        return "error: docker no disponible"


async def _broker_ok(path: Path) -> str:
    if not path.exists():
        return f"error: broker sin socket ({path})"
    try:
        _, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 2)
        writer.close()
        return "ok"
    except (OSError, TimeoutError) as exc:
        return f"error: broker no responde: {exc}"


def _tcp_ok(url: str) -> str:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=2):
            return "ok"
    except socket.gaierror:
        return "unknown: no resoluble desde aquí"
    except OSError as exc:
        return f"error: {exc}"


# --- API (socket Unix) ---------------------------------------------------------------------------

def _opts_from(body: dict[str, Any], channel: str) -> SessionOptions:
    if not isinstance(body.get("task"), str) or not body["task"].strip():
        raise ValueError("falta 'task'")
    return SessionOptions(
        task=body["task"], profile=body.get("profile", "personal"),
        channel=body.get("channel", channel), dry_run=body.get("dry_run"),
        allow_domains=list(body.get("allow_domains") or []),
        session_budget_tokens=body.get("budget_tokens"),
        thread_id=body.get("thread_id"))


def build_api(core: Core) -> Starlette:
    async def health(_: Request) -> JSONResponse:
        return JSONResponse(await core.health())

    async def state(_: Request) -> JSONResponse:
        """Contadores baratos para sondeo frecuente (consolas); /health es el chequeo completo."""
        return JSONResponse({"running_sessions": len(core.manager.running()),
                             "pending_approvals": len(core.hub.pending),
                             "kill_switch": core.kill.active()})

    async def list_sessions(_: Request) -> JSONResponse:
        running = {s.session_id for s in core.manager.running()}
        rows = core.store.sessions(50)
        for row in rows:
            row["live"] = row["id"] in running
        return JSONResponse(rows)

    async def create_session(request: Request) -> JSONResponse:
        try:
            opts = _opts_from(await request.json(), "api")
            if opts.thread_id:
                core.state.thread(opts.thread_id)   # KeyError => 400
            sid = core.manager.start(opts)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": str(exc)}, 400)
        except SessionRefused as exc:
            return JSONResponse({"error": str(exc)}, 409)
        return JSONResponse({"session_id": sid}, 201)

    async def get_session(request: Request) -> JSONResponse:
        sid = request.path_params["sid"]
        entry = core.manager.sessions.get(sid)
        rows = [r for r in core.store.sessions(500) if r["id"] == sid]
        if not rows and not entry:
            return JSONResponse({"error": "no existe"}, 404)
        return JSONResponse({"session": rows[0] if rows else None,
                             "live": bool(entry and not entry.task.done()),
                             "result": entry.result if entry else None})

    async def cancel_session(request: Request) -> JSONResponse:
        ok = core.manager.cancel(request.path_params["sid"])
        return JSONResponse({"cancelled": ok}, 200 if ok else 404)

    async def stream_events(request: Request) -> StreamingResponse:
        sid = request.path_params["sid"]
        replay = request.query_params.get("replay", "1") != "0"

        async def gen() -> AsyncIterator[bytes]:
            queue = core.bus.subscribe(sid)   # antes del replay: no se pierde nada
            seen: set[str] = set()
            entry = core.manager.sessions.get(sid)
            if entry is None and not core.store.events(sid, ["session"]):
                yield _sse({"type": "stream_end", "session_id": sid, "status": "unknown",
                            "message": "sesión desconocida"})
                core.bus.unsubscribe(sid, queue)
                return
            try:
                if replay:
                    for ev in core.store.events(sid):
                        data = ev.model_dump(mode="json")
                        seen.add(data["id"])
                        yield _sse(data)
                        if data["type"] == "session_end":
                            return
                    for pending in core.hub.pending.values():
                        if core.bus.root_of(pending.session_id) == sid:
                            yield _sse({"type": "approval_request", **pending.as_dict()})
                idle = 0
                while True:
                    try:
                        data = await asyncio.wait_for(queue.get(), 1)
                    except TimeoutError:
                        # La tarea pudo terminar sin session_end (p. ej. cancelada antes de
                        # arrancar): se cierra el flujo con su resultado real.
                        if entry is not None and entry.task.done() and queue.empty():
                            yield _sse({"type": "stream_end", "session_id": sid,
                                        **(entry.result or {"status": "unknown"})})
                            return
                        idle += 1
                        if idle % 15 == 0:
                            yield b": keepalive\n\n"
                        continue
                    if data.get("id") in seen:
                        continue
                    yield _sse(data)
                    if data.get("type") == "session_end" and data.get("session_id") == sid:
                        return
            finally:
                core.bus.unsubscribe(sid, queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def stream_all(_: Request) -> StreamingResponse:
        """Todos los eventos de todas las sesiones en vivo (consolas y canales globales)."""
        async def gen() -> AsyncIterator[bytes]:
            queue = core.bus.subscribe("*")
            try:
                for pending in list(core.hub.pending.values()):
                    yield _sse({"type": "approval_request", **pending.as_dict()})
                while True:
                    try:
                        yield _sse(await asyncio.wait_for(queue.get(), 15))
                    except TimeoutError:
                        yield b": keepalive\n\n"
            finally:
                core.bus.unsubscribe("*", queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def create_thread(request: Request) -> JSONResponse:
        body = await request.json()
        profile = body.get("profile", "personal")
        if not core.cfg.allows_profile(profile):
            return JSONResponse({"error": f"perfil {profile!r} fuera del segmento"}, 409)
        t = core.state.create_thread(str(body.get("title") or "conversación"),
                                     str(body.get("channel") or "api"), profile)
        return JSONResponse(t.__dict__, 201)

    async def list_memory(request: Request) -> JSONResponse:
        profile = request.query_params.get("profile")
        return JSONResponse([m.as_dict() for m in core.state.memories(profile, 50)])

    async def list_threads(_: Request) -> JSONResponse:
        return JSONResponse([t.__dict__ for t in core.state.threads(30)])

    async def list_approvals(_: Request) -> JSONResponse:
        return JSONResponse([p.as_dict() for p in core.hub.pending.values()])

    async def decide_approval(request: Request) -> JSONResponse:
        body = await request.json()
        decision = body.get("decision")
        if decision not in ("approved", "denied"):
            return JSONResponse({"error": "decision debe ser approved|denied"}, 400)
        ok = core.hub.decide(request.path_params["aid"], decision,
                             str(body.get("approver", "api-user"))[:64],
                             str(body.get("channel", "api"))[:32])
        return JSONResponse({"ok": ok}, 200 if ok else 404)

    async def kill(request: Request) -> JSONResponse:
        body = await request.json() if await request.body() else {}
        core.kill.engage(str(body.get("reason", "api")))
        return JSONResponse({"kill_switch": True})

    async def rearm(_: Request) -> JSONResponse:
        core.kill.rearm()
        return JSONResponse({"kill_switch": False})

    async def schedules(_: Request) -> JSONResponse:
        return JSONResponse(core.scheduler.status())

    async def run_schedule(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        sch = next((s for s in core.scheduler.sched.schedules if s.name == name), None)
        if sch is None:
            return JSONResponse({"error": "no existe"}, 404)
        sid = core.scheduler.fire(sch, reason="manual")
        return JSONResponse({"session_id": sid, "skipped": sid is None})

    return Starlette(routes=[
        Route("/health", health),
        Route("/state", state),
        Route("/sessions", list_sessions, methods=["GET"]),
        Route("/sessions", create_session, methods=["POST"]),
        Route("/sessions/{sid}", get_session),
        Route("/sessions/{sid}/cancel", cancel_session, methods=["POST"]),
        Route("/sessions/{sid}/events", stream_events),
        Route("/events", stream_all),
        Route("/threads", list_threads, methods=["GET"]),
        Route("/memory", list_memory),
        Route("/threads", create_thread, methods=["POST"]),
        Route("/approvals", list_approvals),
        Route("/approvals/{aid}", decide_approval, methods=["POST"]),
        Route("/kill", kill, methods=["POST"]),
        Route("/rearm", rearm, methods=["POST"]),
        Route("/schedules", schedules),
        Route("/schedules/{name}/run", run_schedule, methods=["POST"]),
    ])


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


# --- webhooks (TCP) ------------------------------------------------------------------------------

def build_hooks(core: Core) -> Starlette:
    hooks = {h.name: h for h in core.scheduler.sched.hooks if h.enabled}

    async def receive(request: Request) -> JSONResponse:
        hook = hooks.get(request.path_params["name"])
        expected = os.environ.get(hook.token_env, "") if hook else ""
        given = request.headers.get("x-argos-token", "")
        # Misma respuesta para hook inexistente, desactivado o token malo: no revela cuáles hay.
        if not hook or not expected or not hmac.compare_digest(given, expected):
            return JSONResponse({"error": "no autorizado"}, 401)
        raw = await request.body()
        if len(raw) > 64_000:
            return JSONResponse({"error": "payload demasiado grande"}, 413)
        try:
            payload: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        try:
            sid = core.manager.start(
                SessionOptions(task=render_hook_task(hook, payload), profile=hook.profile,
                               channel="webhook"),
                interactive=False, tags={"hook": hook.name})
        except SessionRefused as exc:
            return JSONResponse({"error": str(exc)}, 409)
        core.scheduler._log(hook=hook.name, action="fire", session_id=sid)
        return JSONResponse({"session_id": sid}, 202)

    return Starlette(routes=[Route("/hooks/{name}", receive, methods=["POST"])])


# --- arranque ------------------------------------------------------------------------------------

async def serve(core: Core, socket_path: Path, hooks_host: str | None, hooks_port: int | None,
                scheduler_interval_s: float = 15, run_scheduler: bool = True) -> None:
    import uvicorn

    # Directorio privado: aunque el socket naciera con permisos amplios, nadie más llega a él.
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(socket_path.parent, 0o700)
    socket_path.unlink(missing_ok=True)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await core.manager.shutdown()

    api = build_api(core)
    api.router.lifespan_context = lifespan
    servers = [uvicorn.Server(uvicorn.Config(api, uds=str(socket_path), log_level="warning"))]
    if hooks_port:
        servers.append(uvicorn.Server(uvicorn.Config(
            build_hooks(core), host=hooks_host or "127.0.0.1", port=hooks_port,
            log_level="warning")))

    async def restrict_socket() -> None:
        for _ in range(100):
            if socket_path.exists():
                os.chmod(socket_path, 0o600)   # la autenticación de la API es este permiso
                return
            await asyncio.sleep(0.05)

    sched_task = (asyncio.create_task(core.scheduler.run(scheduler_interval_s))
                  if run_scheduler else None)
    try:
        await asyncio.gather(*(s.serve() for s in servers), restrict_socket())
    finally:
        if sched_task:
            sched_task.cancel()
        socket_path.unlink(missing_ok=True)
