"""Puente Matrix ↔ núcleo persistente (RF-07, RF-20). Es un cliente más de la API (P1).

- Solo obedece a `allowed_users` y solo acepta invitaciones suyas: un bot de Matrix recibe
  mensajes de cualquiera que lo invite.
- Un hilo de Matrix = una conversación de Argos (memoria y contexto compartidos).
- Progreso en un único mensaje editado; la respuesta final, en un mensaje nuevo (notifica).
- Aprobaciones: responder «sí»/«no» en el hilo o reaccionar ✅/❌ a la solicitud. Las de otros
  canales pueden llegar a `notify_room` para aprobar desde el móvil.
- Comandos: !estado, !memoria, !kill <motivo>, !rearm, !ayuda.
- Otros segmentos (ADR-0026): `!osint <tarea>` / `!pentest <tarea>` abren un hilo atendido por
  el daemon de ese segmento. El puente solo transporta texto; el orquestador nunca lo lee (P2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argos.channels.matrix.client import MatrixClient, MatrixError
from argos.server.client import CoreClient, CoreUnavailable

log = logging.getLogger("argos.matrix")

YES = {"si", "sí", "s", "yes", "y", "ok", "vale", "aprueba", "aprobar", "✅", "👍"}
NO = {"no", "n", "deniega", "denegar", "❌", "👎"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (room_id TEXT, root_event TEXT, argos_thread TEXT,
    PRIMARY KEY (room_id, root_event));
CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, room_id TEXT,
    event_id TEXT, thread_root TEXT, session_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""
# Columnas añadidas después (ADR-0026): segmento y perfil de cada hilo / aprobación.
MIGRATIONS = (
    "ALTER TABLE threads ADD COLUMN segment TEXT DEFAULT 'main'",
    "ALTER TABLE threads ADD COLUMN profile TEXT",
    "ALTER TABLE approvals ADD COLUMN segment TEXT DEFAULT 'main'",
)
MAIN = "main"


@dataclass
class BridgeConfig:
    allowed_users: list[str]
    profile: str = "personal"
    notify_room: str | None = None
    progress_interval_s: float = 3.0
    # Abre (una vez) un chat directo SIN cifrar con el primer usuario autorizado: los DMs que
    # crea Element nacen cifrados y el puente no puede leerlos (ADR-0012).
    open_dm: bool = True
    # Perfiles de otros segmentos accesibles con !<perfil> → su segmento (p. ej. osint → osint).
    segment_profiles: dict[str, str] = field(default_factory=dict)


@dataclass
class _Run:
    session_id: str
    room_id: str
    thread_root: str
    progress_event: str
    lines: list[str] = field(default_factory=list)
    last_edit: float = 0.0
    segment: str = MAIN
    handoff: dict[str, Any] | None = None  # agent.handoff → reenviar a otro segmento (ADR-0026)


class MatrixBridge:
    def __init__(
        self,
        matrix: MatrixClient,
        core: CoreClient,
        cfg: BridgeConfig,
        db_path: Path,
        segment_client: Callable[[str], CoreClient] | None = None,
    ) -> None:
        self.matrix = matrix
        self.core = core
        self.cfg = cfg
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            with contextlib.suppress(sqlite3.OperationalError):  # ya aplicada
                self.db.execute(stmt)
        self.db.commit()
        self._segment_client = segment_client
        self._cores: dict[str, CoreClient] = {MAIN: core}
        self.runs: dict[str, _Run] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self._watcher: asyncio.Task[Any] | None = None

    def _core(self, segment: str) -> CoreClient:
        """Cliente del daemon de ese segmento (se crea al primer uso; si su socket no existe,
        CoreUnavailable y se reintenta la próxima vez)."""
        if segment not in self._cores:
            if self._segment_client is None:
                raise CoreUnavailable(f"sin acceso al núcleo del segmento {segment}")
            self._cores[segment] = self._segment_client(segment)
        return self._cores[segment]

    async def aclose(self) -> None:
        for seg, client in self._cores.items():
            if seg != MAIN:
                await client.__aexit__(None, None, None)

    # --- persistencia mínima -----------------------------------------------------------------

    def _kv(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def _thread_for(self, room_id: str, root: str) -> tuple[str, str, str] | None:
        """(hilo de Argos, segmento, perfil) del hilo de Matrix, si ya existe."""
        row = self.db.execute(
            "SELECT argos_thread, segment, profile FROM threads WHERE room_id=? AND root_event=?",
            (room_id, root),
        ).fetchone()
        if not row:
            return None
        return row[0], row[1] or MAIN, row[2] or self.cfg.profile

    def _approval_by_event(self, event_id: str) -> str | None:
        row = self.db.execute(
            "SELECT approval_id FROM approvals WHERE event_id=?", (event_id,)
        ).fetchone()
        return row[0] if row else None

    def _pending_in_thread(self, room_id: str, root: str) -> str | None:
        row = self.db.execute(
            "SELECT approval_id FROM approvals WHERE room_id=? AND thread_root=?"
            " ORDER BY created DESC LIMIT 1",
            (room_id, root),
        ).fetchone()
        return row[0] if row else None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- bucle principal ---------------------------------------------------------------------

    async def run(self, once: bool = False) -> None:
        me = await self.matrix.whoami()
        log.info("puente Matrix como %s; usuarios autorizados: %s", me, self.cfg.allowed_users)
        if self.cfg.open_dm and self.cfg.allowed_users:
            await self.ensure_dm(self.cfg.allowed_users[0])
        since = self._kv("since")
        if since is None:
            # Primer arranque: no se ejecutan mensajes antiguos, solo se marca el punto de partida.
            first = await self.matrix.sync(None)
            since = self._kv("since", first["next_batch"])
            await self._handle_invites(first)
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch_global())
        while True:
            try:
                data = await self.matrix.sync(since, timeout_ms=1000 if once else 30_000)
            except (MatrixError, OSError) as exc:
                log.warning("sync falló: %s; reintento en 5 s", exc)
                if once:
                    raise
                await asyncio.sleep(5)
                continue
            await self._handle_invites(data)
            for room_id, room in (data.get("rooms", {}).get("join") or {}).items():
                for ev in room.get("timeline", {}).get("events", []):
                    try:
                        await self._handle_event(room_id, ev)
                    except Exception:  # noqa: BLE001 — un evento raro no tumba el puente
                        log.exception("error procesando evento %s", ev.get("event_id"))
            since = self._kv("since", data["next_batch"])
            if once:
                return

    async def ensure_dm(self, user_id: str) -> str:
        """Chat directo sin cifrar con `user_id`; lo crea e invita la primera vez."""
        key = f"dm:{user_id}"
        if room := self._kv(key):
            return room
        room = await self.matrix.create_dm(
            user_id, topic="Argos · sin cifrar (el bot no soporta E2EE)"
        )
        self._kv(key, room)
        log.info("chat directo %s creado con %s", room, user_id)
        await self.matrix.send_text(
            room,
            "Hola, soy Argos. Escríbeme aquí una tarea y la haré en un hilo; en el hilo "
            "seguimos la conversación y te pediré las aprobaciones. !ayuda para comandos.\n"
            "Este chat NO está cifrado a propósito (el bot aún no soporta E2EE); no "
            "actives el cifrado o dejaré de leerte.",
        )
        return room

    async def _handle_invites(self, data: dict[str, Any]) -> None:
        for room_id, room in (data.get("rooms", {}).get("invite") or {}).items():
            events = room.get("invite_state", {}).get("events", [])
            inviter = next(
                (
                    e.get("sender")
                    for e in events
                    if e.get("type") == "m.room.member"
                    and e.get("state_key") == self.matrix.user_id
                ),
                None,
            )
            if inviter in self.cfg.allowed_users:
                await self.matrix.join(room_id)
                log.info("unido a %s por invitación de %s", room_id, inviter)
            else:
                log.warning("invitación ignorada de %s a %s", inviter, room_id)

    async def _handle_event(self, room_id: str, ev: dict[str, Any]) -> None:
        sender = ev.get("sender")
        if sender == self.matrix.user_id or sender not in self.cfg.allowed_users:
            return
        if ev.get("type") == "m.room.encrypted":
            await self._warn_encrypted(room_id)
            return
        content = ev.get("content") or {}
        rel = content.get("m.relates_to") or {}
        if ev.get("type") == "m.reaction":
            if rel.get("rel_type") == "m.annotation" and (
                aid := self._approval_by_event(rel.get("event_id", ""))
            ):
                await self._decide(room_id, aid, rel.get("key", ""), sender)
            return
        if content.get("msgtype") not in ("m.text", None) or "m.new_content" in content:
            return  # ediciones, imágenes, avisos: se ignoran
        body = (content.get("body") or "").strip()
        if not body:
            return
        root = rel.get("event_id") if rel.get("rel_type") == "m.thread" else ev["event_id"]

        if body.startswith("!"):
            await self._command(room_id, root, body, sender)
            return
        pending = self._pending_in_thread(room_id, root)
        if pending and body.lower().strip(".! ") in YES | NO:
            await self._decide(room_id, pending, body, sender)
            return
        await self._submit(room_id, root, body)

    async def _warn_encrypted(self, room_id: str) -> None:
        """Una vez por sala: el bot no puede leer mensajes cifrados; mejor decirlo que callar."""
        if self._kv(f"warned:{room_id}"):
            return
        self._kv(f"warned:{room_id}", "1")
        dm = next(
            (self._kv(f"dm:{u}") for u in self.cfg.allowed_users if self._kv(f"dm:{u}")), None
        )
        where = (
            " Escríbeme en el chat «Argos», que está sin cifrar." if dm and dm != room_id else ""
        )
        with contextlib.suppress(MatrixError):
            await self.matrix.send_text(
                room_id,
                "🔒 Esta sala está cifrada y no puedo leer tus mensajes (aún no soporto "
                "E2EE)." + where,
                notice=True,
            )

    # --- tareas ------------------------------------------------------------------------------

    async def _submit(
        self,
        room_id: str,
        root: str,
        task: str,
        segment: str | None = None,
        profile: str | None = None,
    ) -> None:
        """Envía la tarea al daemon que atiende el hilo. Un hilo nuevo va al orquestador (main)
        salvo que se pida otro segmento (!osint …); un hilo existente sigue donde empezó."""
        existing = self._thread_for(room_id, root)
        if existing and segment and existing[1] != segment:
            await self.matrix.send_text(
                room_id,
                f"⚠️ Este hilo lo atiende {existing[2]}; escribe !{profile} fuera de un hilo.",
                thread_root=root,
                notice=True,
            )
            return
        if existing:
            thread, segment, profile = existing
        else:
            segment, profile = segment or MAIN, profile or self.cfg.profile
        try:
            core = self._core(segment)
            if not existing:
                thread = (await core.create_thread(task[:60], profile, "matrix"))["id"]
                self.db.execute(
                    "INSERT INTO threads (room_id, root_event, argos_thread, segment, profile)"
                    " VALUES (?,?,?,?,?)",
                    (room_id, root, thread, segment, profile),
                )
                self.db.commit()
            sid = await core.submit(task=task, profile=profile, channel="matrix", thread_id=thread)
        except CoreUnavailable:
            msg = f"⚠️ El núcleo de {segment} no está en marcha."
            await self.matrix.send_text(room_id, msg, thread_root=root, notice=True)
            return
        except RuntimeError as exc:
            await self.matrix.send_text(room_id, f"⚠️ {exc}", thread_root=root, notice=True)
            return
        where = "" if segment == MAIN else f" ({profile})"
        progress = await self.matrix.send_text(
            room_id, f"⏳ trabajando…{where}", thread_root=root, notice=True
        )
        run = _Run(sid, room_id, root, progress, segment=segment)
        self.runs[sid] = run
        self._spawn(self._follow(run))

    async def _follow(self, run: _Run) -> None:
        final = None
        try:
            async for ev in self._core(run.segment).events(run.session_id):
                kind = ev.get("type")
                if kind == "approval_request":
                    await self._post_approval(ev, run.room_id, run.thread_root, run.segment)
                    continue
                if kind == "handoff":
                    run.handoff = ev
                    continue
                if line := progress_line(ev, run.session_id):
                    run.lines.append(line)
                    await self._maybe_edit(run)
                if kind in ("session_end", "stream_end") and ev.get("session_id") == run.session_id:
                    final = ev
        finally:
            self.runs.pop(run.session_id, None)
        status = (final or {}).get("status", "desconocido")
        icon = "✅" if status == "completed" else "⚠️"
        steps = (final or {}).get("steps")
        run.lines.append(f"{icon} {status}" + (f" · {steps} pasos" if steps else ""))
        await self._maybe_edit(run, force=True)
        answer = (final or {}).get("result") or (final or {}).get("message") or ""
        if answer:
            await self.matrix.send_text(run.room_id, answer, thread_root=run.thread_root)
        if run.handoff:
            # El orquestador pasó la tarea a un perfil aislado (ADR-0026). Reenvío al daemon de ese
            # segmento; el hilo pasa a ser de ese perfil y su resultado va al usuario, no al
            # orquestador. Rebind: borro la vinculación del hilo para que _submit cree la nueva.
            h = run.handoff
            self.db.execute(
                "DELETE FROM threads WHERE room_id=? AND root_event=?",
                (run.room_id, run.thread_root),
            )
            self.db.commit()
            await self._submit(
                run.room_id, run.thread_root, h["task"], h["target_segment"], h["target_profile"]
            )

    async def _maybe_edit(self, run: _Run, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - run.last_edit < self.cfg.progress_interval_s:
            return
        run.last_edit = now
        text = "\n".join(run.lines[-12:]) or "⏳ trabajando…"
        with contextlib.suppress(MatrixError):
            await self.matrix.edit_text(run.room_id, run.progress_event, text)

    # --- aprobaciones (RF-20) ----------------------------------------------------------------

    async def _post_approval(
        self, ev: dict[str, Any], room_id: str, root: str | None, segment: str = MAIN
    ) -> None:
        aid = ev["id"]
        if self.db.execute("SELECT 1 FROM approvals WHERE approval_id=?", (aid,)).fetchone():
            return
        where = "" if segment == MAIN else f" [{segment}]"
        text = (
            f"🔐 Aprobación necesaria{where} · {ev['action']} (riesgo {ev['risk_class']}, "
            f"caduca en {ev['timeout_s']} s)\n{ev['details']}\n\n"
            "Responde «sí» o «no» en este hilo, o reacciona ✅ / ❌."
        )
        event_id = await self.matrix.send_text(room_id, text, thread_root=root)
        self.db.execute(
            "INSERT INTO approvals (approval_id, room_id, event_id, thread_root, session_id,"
            " created, segment) VALUES (?,?,?,?,?,?,?)",
            (aid, room_id, event_id, root or event_id, ev.get("session_id"), time.time(), segment),
        )
        self.db.commit()

    async def _decide(self, room_id: str, approval_id: str, answer: str, sender: str) -> None:
        word = answer.lower().strip(".! ")
        if word not in YES | NO:
            return
        decision = "approved" if word in YES else "denied"
        row = self.db.execute(
            "SELECT thread_root, segment FROM approvals WHERE approval_id=?", (approval_id,)
        ).fetchone()
        try:
            core = self._core((row[1] if row else None) or MAIN)
            ok = await core.decide(approval_id, decision, sender, "matrix")
        except CoreUnavailable:
            ok = False
        self.db.execute("DELETE FROM approvals WHERE approval_id=?", (approval_id,))
        self.db.commit()
        msg = (
            f"{'✅ aprobada' if decision == 'approved' else '❌ denegada'} por {sender}"
            if ok
            else "ya estaba resuelta (o caducó)"
        )
        await self.matrix.send_text(room_id, msg, thread_root=row[0] if row else None, notice=True)

    def _report_room(self) -> str | None:
        """Dónde avisar de lo que no nace en Matrix: sala de control o el chat directo."""
        if self.cfg.notify_room:
            return self.cfg.notify_room
        return next((r for u in self.cfg.allowed_users if (r := self._kv(f"dm:{u}"))), None)

    async def _watch_global(self) -> None:
        """Flujo global del núcleo: aprobaciones de otros canales → sala de control, y el
        resultado de cada tarea programada → chat de avisos (notifica en el móvil)."""
        scheduled: set[str] = set()
        while True:
            try:
                async for ev in self.core.events_all():
                    kind = ev.get("type")
                    if kind == "approval_request":
                        if self.cfg.notify_room and ev.get("origin_channel") != "matrix":
                            await self._post_approval(ev, self.cfg.notify_room, None)
                    elif (
                        kind == "session"
                        and ev.get("channel") == "scheduler"
                        and not ev.get("parent_session_id")
                    ):
                        scheduled.add(ev["session_id"])
                    elif kind == "eval_report" and ev.get("notify", True):
                        if room := self._report_room():
                            await self.matrix.send_text(
                                room, f"📊 {ev.get('title', 'Evaluación')}\n{ev.get('text', '')}"
                            )
                    elif kind == "session_end" and ev.get("session_id") in scheduled:
                        scheduled.discard(ev["session_id"])
                        await self._report_scheduled(ev)
            except Exception as exc:  # noqa: BLE001 — el núcleo puede reiniciarse
                log.warning("flujo global interrumpido: %s; reintento", exc)
                await asyncio.sleep(5)

    async def _report_scheduled(self, ev: dict[str, Any]) -> None:
        room = self._report_room()
        if not room:
            return
        sid = ev.get("session_id")
        sched = next((s for s in await self.core.schedules() if s.get("last_session") == sid), {})
        if sched and not sched.get("notify", True):
            return
        title = sched.get("title") or sched.get("name") or "tarea programada"
        status = ev.get("status")
        answer = (ev.get("result") or "").strip() or "(sin respuesta)"
        head = f"⏰ {title}" + ("" if status == "completed" else f" · ⚠️ {status}")
        await self.matrix.send_text(room, f"{head}\n{answer}")

    # --- comandos ------------------------------------------------------------------------------

    def _segments(self) -> list[str]:
        return [MAIN, *dict.fromkeys(self.cfg.segment_profiles.values())]

    async def _each_segment(self, fn) -> list[str]:
        """Aplica `fn(core)` en cada daemon; una línea por segmento (los parados se indican)."""
        lines = []
        for seg in self._segments():
            try:
                lines.append(f"{seg}: {await fn(self._core(seg))}")
            except CoreUnavailable:
                lines.append(f"{seg}: no está en marcha")
            except RuntimeError as exc:
                lines.append(f"{seg}: ⚠️ {exc}")
        return lines

    async def _command(self, room_id: str, root: str, body: str, sender: str) -> None:
        cmd, _, arg = body[1:].partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in self.cfg.segment_profiles:
            if arg:
                await self._submit(room_id, root, arg, self.cfg.segment_profiles[cmd], cmd)
                return
            text = f"uso: !{cmd} <tarea>. Abre un hilo con el perfil {cmd}; sigue en el hilo."
        elif cmd in ("estado", "status"):

            async def state(core: CoreClient) -> str:
                st = await core.state()
                return (
                    f"sesiones {st['running_sessions']} · aprobaciones {st['pending_approvals']}"
                    f" · kill switch {st['kill_switch'] or 'no'}"
                )

            text = "\n".join(await self._each_segment(state))
        elif cmd in ("kill", "parar"):

            async def kill(core: CoreClient) -> str:
                await core.kill(arg or f"matrix:{sender}")
                return "detenido"

            lines = await self._each_segment(kill)
            text = "🛑 kill switch activado (`!rearm` para rearmar):\n" + "\n".join(lines)
        elif cmd == "rearm":

            async def rearm(core: CoreClient) -> str:
                await core.rearm()
                return "rearmado"

            text = "\n".join(await self._each_segment(rearm))
        elif cmd in ("memoria", "memory", "herramientas", "tools"):
            # Opcional: el perfil de otro segmento (!herramientas osint).
            profile = arg if arg in self.cfg.segment_profiles else self.cfg.profile
            seg = self.cfg.segment_profiles.get(profile, MAIN)
            try:
                core = self._core(seg)
                if cmd in ("memoria", "memory"):
                    items = await core.memories(profile)
                    text = (
                        "\n".join(
                            f"• {m['content']} ({'tú' if m['provenance'] == 'user' else 'agente'})"
                            for m in items[:20]
                        )
                        or "(sin memoria)"
                    )
                else:
                    items = await core.tools(profile)
                    text = "\n".join(f"• {t['name']} ({t['risk']})" for t in items) or "(sin tools)"
            except CoreUnavailable:
                text = f"el núcleo de {seg} no está en marcha"
        else:
            others = " · ".join(f"!{p} <tarea>" for p in self.cfg.segment_profiles)
            text = (
                "Escríbeme una tarea y la hago en un hilo. En el hilo sigue la conversación.\n"
                + (f"{others} (su propio núcleo, aislado)\n" if others else "")
                + "!estado · !memoria [perfil] · !herramientas [perfil] · !kill <motivo> · !rearm"
            )
        await self.matrix.send_text(room_id, text, thread_root=root, notice=True)


def progress_line(ev: dict[str, Any], root: str) -> str | None:
    """Línea de progreso en texto plano (Matrix no interpreta el marcado de rich)."""
    kind = ev.get("type")
    pad = "  ↳ " if ev.get("session_id") != root else "· "
    if kind == "turn" and ev.get("purpose") == "decide":
        d = ev.get("decision") or {}
        if d.get("type") == "tool_call":
            args = json.dumps(d.get("args") or {}, ensure_ascii=False)
            return f"{pad}{d.get('tool')} {args[:80] + ('…' if len(args) > 80 else '')}"
    if kind == "tool_call" and ev.get("status") in ("error", "denied"):
        return f"{pad}✗ {(ev.get('result_preview') or '')[:100]}"
    if kind == "memory_event" and ev.get("op") == "save":
        return f"{pad}recordado: {ev.get('detail', '')[:90]}"
    if kind == "approval":
        return f"{pad}aprobación {ev['decision']} ({ev['approver']})"
    if kind == "error_event" and ev.get("kind") in (
        "budget_exceeded",
        "loop_detected",
        "model_error",
        "killed",
        "egress_blocked",
    ):
        return f"{pad}⚠️ {ev['kind']}: {ev['message'][:100]}"
    return None
