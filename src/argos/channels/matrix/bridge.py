"""Puente Matrix ↔ núcleo persistente (RF-07, RF-20). Es un cliente más de la API (P1).

- Solo obedece a `allowed_users` y solo acepta invitaciones suyas: un bot de Matrix recibe
  mensajes de cualquiera que lo invite.
- Un hilo de Matrix = una conversación de Argos (memoria y contexto compartidos).
- Progreso en un único mensaje editado; la respuesta final, en un mensaje nuevo (notifica).
- Aprobaciones: responder «sí»/«no» en el hilo o reaccionar ✅/❌ a la solicitud. Las de otros
  canales pueden llegar a `notify_room` para aprobar desde el móvil.
- Comandos: !estado, !memoria, !kill <motivo>, !rearm, !ayuda.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argos.channels.matrix.client import MatrixClient, MatrixError
from argos.server.client import CoreClient

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


@dataclass
class BridgeConfig:
    allowed_users: list[str]
    profile: str = "personal"
    notify_room: str | None = None
    progress_interval_s: float = 3.0
    # Abre (una vez) un chat directo SIN cifrar con el primer usuario autorizado: los DMs que
    # crea Element nacen cifrados y el puente no puede leerlos (ADR-0012).
    open_dm: bool = True


@dataclass
class _Run:
    session_id: str
    room_id: str
    thread_root: str
    progress_event: str
    lines: list[str] = field(default_factory=list)
    last_edit: float = 0.0


class MatrixBridge:
    def __init__(self, matrix: MatrixClient, core: CoreClient, cfg: BridgeConfig,
                 db_path: Path) -> None:
        self.matrix = matrix
        self.core = core
        self.cfg = cfg
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.runs: dict[str, _Run] = {}
        self.tasks: set[asyncio.Task[Any]] = set()

    # --- persistencia mínima -----------------------------------------------------------------

    def _kv(self, key: str, value: str | None = None) -> str | None:
        if value is not None:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))
            self.db.commit()
            return value
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def _thread_for(self, room_id: str, root: str) -> str | None:
        row = self.db.execute("SELECT argos_thread FROM threads WHERE room_id=? AND root_event=?",
                              (room_id, root)).fetchone()
        return row[0] if row else None

    def _approval_by_event(self, event_id: str) -> str | None:
        row = self.db.execute("SELECT approval_id FROM approvals WHERE event_id=?",
                              (event_id,)).fetchone()
        return row[0] if row else None

    def _pending_in_thread(self, room_id: str, root: str) -> str | None:
        row = self.db.execute(
            "SELECT approval_id FROM approvals WHERE room_id=? AND thread_root=?"
            " ORDER BY created DESC LIMIT 1", (room_id, root)).fetchone()
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
        if self.cfg.notify_room:
            self._spawn(self._forward_foreign_approvals())
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
            user_id, topic="Argos · sin cifrar (el bot no soporta E2EE)")
        self._kv(key, room)
        log.info("chat directo %s creado con %s", room, user_id)
        await self.matrix.send_text(
            room, "Hola, soy Argos. Escríbeme aquí una tarea y la haré en un hilo; en el hilo "
                  "seguimos la conversación y te pediré las aprobaciones. !ayuda para comandos.\n"
                  "Este chat NO está cifrado a propósito (el bot aún no soporta E2EE); no "
                  "actives el cifrado o dejaré de leerte.")
        return room

    async def _handle_invites(self, data: dict[str, Any]) -> None:
        for room_id, room in (data.get("rooms", {}).get("invite") or {}).items():
            events = room.get("invite_state", {}).get("events", [])
            inviter = next((e.get("sender") for e in events
                            if e.get("type") == "m.room.member"
                            and e.get("state_key") == self.matrix.user_id), None)
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
            if rel.get("rel_type") == "m.annotation" and (aid := self._approval_by_event(
                    rel.get("event_id", ""))):
                await self._decide(room_id, aid, rel.get("key", ""), sender)
            return
        if content.get("msgtype") not in ("m.text", None) or "m.new_content" in content:
            return   # ediciones, imágenes, avisos: se ignoran
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
        dm = next((self._kv(f"dm:{u}") for u in self.cfg.allowed_users if self._kv(f"dm:{u}")),
                  None)
        where = " Escríbeme en el chat «Argos», que está sin cifrar." if dm and dm != room_id \
            else ""
        with contextlib.suppress(MatrixError):
            await self.matrix.send_text(
                room_id, "🔒 Esta sala está cifrada y no puedo leer tus mensajes (aún no soporto "
                         "E2EE)." + where, notice=True)

    # --- tareas ------------------------------------------------------------------------------

    async def _submit(self, room_id: str, root: str, task: str) -> None:
        thread = self._thread_for(room_id, root)
        if thread is None:
            thread = (await self.core.create_thread(task[:60], self.cfg.profile, "matrix"))["id"]
            self.db.execute("INSERT INTO threads VALUES (?,?,?)", (room_id, root, thread))
            self.db.commit()
        try:
            sid = await self.core.submit(task=task, profile=self.cfg.profile, channel="matrix",
                                         thread_id=thread)
        except RuntimeError as exc:
            await self.matrix.send_text(room_id, f"⚠️ {exc}", thread_root=root, notice=True)
            return
        progress = await self.matrix.send_text(room_id, "⏳ trabajando…", thread_root=root,
                                               notice=True)
        run = _Run(sid, room_id, root, progress)
        self.runs[sid] = run
        self._spawn(self._follow(run))

    async def _follow(self, run: _Run) -> None:
        final = None
        try:
            async for ev in self.core.events(run.session_id):
                kind = ev.get("type")
                if kind == "approval_request":
                    await self._post_approval(ev, run.room_id, run.thread_root)
                    continue
                if line := progress_line(ev, run.session_id):
                    run.lines.append(line)
                    await self._maybe_edit(run)
                if kind in ("session_end", "stream_end") and \
                        ev.get("session_id") == run.session_id:
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

    async def _maybe_edit(self, run: _Run, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - run.last_edit < self.cfg.progress_interval_s:
            return
        run.last_edit = now
        text = "\n".join(run.lines[-12:]) or "⏳ trabajando…"
        with contextlib.suppress(MatrixError):
            await self.matrix.edit_text(run.room_id, run.progress_event, text)

    # --- aprobaciones (RF-20) ----------------------------------------------------------------

    async def _post_approval(self, ev: dict[str, Any], room_id: str, root: str | None) -> None:
        aid = ev["id"]
        if self.db.execute("SELECT 1 FROM approvals WHERE approval_id=?", (aid,)).fetchone():
            return
        text = (f"🔐 Aprobación necesaria · {ev['action']} (riesgo {ev['risk_class']}, "
                f"caduca en {ev['timeout_s']} s)\n{ev['details']}\n\n"
                "Responde «sí» o «no» en este hilo, o reacciona ✅ / ❌.")
        event_id = await self.matrix.send_text(room_id, text, thread_root=root)
        self.db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)",
                        (aid, room_id, event_id, root or event_id, ev.get("session_id"),
                         time.time()))
        self.db.commit()

    async def _decide(self, room_id: str, approval_id: str, answer: str, sender: str) -> None:
        word = answer.lower().strip(".! ")
        if word not in YES | NO:
            return
        decision = "approved" if word in YES else "denied"
        ok = await self.core.decide(approval_id, decision, sender, "matrix")
        row = self.db.execute("SELECT thread_root FROM approvals WHERE approval_id=?",
                              (approval_id,)).fetchone()
        self.db.execute("DELETE FROM approvals WHERE approval_id=?", (approval_id,))
        self.db.commit()
        msg = (f"{'✅ aprobada' if decision == 'approved' else '❌ denegada'} por {sender}"
               if ok else "ya estaba resuelta (o caducó)")
        await self.matrix.send_text(room_id, msg, thread_root=row[0] if row else None,
                                    notice=True)

    async def _forward_foreign_approvals(self) -> None:
        """Aprobaciones de sesiones de otros canales (CLI, consola) → sala de control."""
        while True:
            try:
                async for ev in self.core.events_all():
                    if ev.get("type") != "approval_request":
                        continue
                    if ev.get("origin_channel") == "matrix":
                        continue   # ya las publica el hilo de su sesión
                    await self._post_approval(ev, self.cfg.notify_room, None)
            except Exception as exc:  # noqa: BLE001 — el núcleo puede reiniciarse
                log.warning("flujo global interrumpido: %s; reintento", exc)
                await asyncio.sleep(5)

    # --- comandos ------------------------------------------------------------------------------

    async def _command(self, room_id: str, root: str, body: str, sender: str) -> None:
        cmd, _, arg = body[1:].partition(" ")
        cmd = cmd.lower()
        if cmd in ("estado", "status"):
            st = await self.core.state()
            text = (f"sesiones en curso: {st['running_sessions']} · aprobaciones pendientes: "
                    f"{st['pending_approvals']} · kill switch: {st['kill_switch'] or 'no'}")
        elif cmd in ("kill", "parar"):
            await self.core.kill(arg or f"matrix:{sender}")
            text = "🛑 kill switch activado: todas las sesiones se detienen. `!rearm` para rearmar."
        elif cmd == "rearm":
            await self.core.rearm()
            text = "sistema rearmado"
        elif cmd in ("memoria", "memory"):
            items = await self.core.memories(self.cfg.profile)
            text = "\n".join(f"• {m['content']} ({'tú' if m['provenance'] == 'user' else 'agente'})"
                             for m in items[:20]) or "(sin memoria)"
        elif cmd in ("herramientas", "tools"):
            items = await self.core.tools(self.cfg.profile)
            text = "\n".join(f"• {t['name']} ({t['risk']})" for t in items) or "(sin tools)"
        else:
            text = ("Escríbeme una tarea y la hago en un hilo. En el hilo sigue la conversación.\n"
                    "!estado · !memoria · !herramientas · !kill <motivo> · !rearm")
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
    if kind == "error_event" and ev.get("kind") in ("budget_exceeded", "loop_detected",
                                                    "model_error", "killed",
                                                    "egress_blocked"):
        return f"{pad}⚠️ {ev['kind']}: {ev['message'][:100]}"
    return None
