"""Almacén de auditoría: JSONL append-only + SQLite para consulta + blobs por hash (ADR-0003)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from argos.audit.events import Event, SessionEnded, SessionStarted, parse_event
from argos.audit.redact import Redactor

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    profile TEXT,
    channel TEXT,
    task TEXT,
    model TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS control (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AuditStore:
    def __init__(self, root: Path, redactor: Redactor | None = None) -> None:
        self.root = root
        self.redactor = redactor or Redactor()
        self.jsonl_dir = root / "audit"
        self.blob_dir = root / "blobs"
        self.jsonl_dir.mkdir(parents=True, exist_ok=True)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(root / "argos.db", check_same_thread=False)
        self.db.executescript(SCHEMA)

    # --- escritura -------------------------------------------------------------------------

    def emit(self, event: Event) -> Event:
        data = self.redactor.redact(event.model_dump(mode="json"))
        line = json.dumps(data, ensure_ascii=False)
        with self._lock:
            with open(self.jsonl_dir / f"{event.session_id}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self.db.execute(
                "INSERT INTO events(id, session_id, type, ts, data) VALUES (?,?,?,?,?)",
                (data["id"], data["session_id"], data["type"], data["ts"], line),
            )
            if isinstance(event, SessionStarted):
                self.db.execute(
                    "INSERT INTO sessions(id, parent_session_id, profile, channel, task, model,"
                    " status, started_at) VALUES (?,?,?,?,?,?,?,?)",
                    (event.session_id, event.parent_session_id, event.agent_profile,
                     event.channel, data["task"], event.model, "running", data["ts"]),
                )
            elif isinstance(event, SessionEnded):
                self.db.execute(
                    "UPDATE sessions SET status=?, ended_at=? WHERE id=?",
                    (event.status, data["ts"], event.session_id),
                )
            self.db.commit()
        return event

    def put_blob(self, content: str | bytes) -> str:
        """Guarda contenido (redactado) por hash; devuelve `sha256:<hex>` (RF-OB-08)."""
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        raw = self.redactor.redact_text(content).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = self.blob_dir / digest[:2] / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        return f"sha256:{digest}"

    def get_blob(self, ref: str) -> str:
        digest = ref.removeprefix("sha256:")
        return (self.blob_dir / digest[:2] / digest).read_text(encoding="utf-8")

    # --- control (kill switch) -------------------------------------------------------------

    def set_control(self, key: str, value: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO control(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()

    def get_control(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM control WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # --- lectura ---------------------------------------------------------------------------

    def events(self, session_id: str, types: list[str] | None = None) -> list[Event]:
        q = "SELECT data FROM events WHERE session_id=?"
        params: list[Any] = [session_id]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            params += types
        q += " ORDER BY ts, rowid"
        return [parse_event(json.loads(r[0])) for r in self.db.execute(q, params)]

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        cur = self.db.execute(
            "SELECT id, parent_session_id, profile, channel, task, model, status, started_at,"
            " ended_at FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur]

    def children(self, session_id: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT id FROM sessions WHERE parent_session_id=?", (session_id,))]

    def resolve_session(self, prefix: str) -> str:
        rows = self.db.execute(
            "SELECT id FROM sessions WHERE id LIKE ?", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            raise KeyError(f"prefijo de sesión {prefix!r} ambiguo o inexistente ({len(rows)})")
        return rows[0][0]

    def iter_all(self, types: list[str]) -> Iterator[Event]:
        q = f"SELECT data FROM events WHERE type IN ({','.join('?' * len(types))}) ORDER BY ts"
        for (data,) in self.db.execute(q, types):
            yield parse_event(json.loads(data))
