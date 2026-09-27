"""Servidor MCP mínimo de recordatorios (UC-4). Valida la integración MCP de Fase 1.

Las anotaciones MCP (`read_only_hint`, `destructive_hint`, `idempotent_hint`) son la declaración
de riesgo/idempotencia que exige RF-21; el cliente de Argos las traduce.

Uso: python -m argos.mcp_servers.reminders.server   (stdio; BD en $ARGOS_REMINDERS_DB)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

VERSION = "0.1.0"
server = MCPServer(name="reminders", version=VERSION)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(os.environ.get("ARGOS_REMINDERS_DB", "reminders.db"))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " text TEXT NOT NULL, due TEXT, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
    return conn


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                         idempotent_hint=False))
def add(text: str, due: str | None = None) -> str:
    """Crea un recordatorio. `due` en ISO-8601 opcional."""
    with _db() as conn:
        cur = conn.execute("INSERT INTO reminders(text, due, created_at) VALUES (?,?,?)",
                           (text, due, datetime.now(UTC).isoformat()))
        return f"recordatorio #{cur.lastrowid} creado"


@server.tool(name="list", annotations=ToolAnnotations(read_only_hint=True, idempotent_hint=True))
def list_reminders(include_done: bool = False) -> str:
    """Lista recordatorios (pendientes por defecto)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, text, due, done FROM reminders"
            + ("" if include_done else " WHERE done=0") + " ORDER BY id").fetchall()
    if not rows:
        return "(sin recordatorios)"
    return "\n".join(f"#{i} [{'x' if d else ' '}] {t}" + (f" (vence {due})" if due else "")
                     for i, t, due, d in rows)


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                         idempotent_hint=True))
def complete(reminder_id: int) -> str:
    """Marca un recordatorio como hecho."""
    with _db() as conn:
        cur = conn.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
    if cur.rowcount == 0:
        raise ValueError(f"no existe el recordatorio #{reminder_id}")
    return f"recordatorio #{reminder_id} completado"


@server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                         idempotent_hint=True))
def delete(reminder_id: int) -> str:
    """Borra un recordatorio (destructivo: requiere aprobación)."""
    with _db() as conn:
        conn.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    return f"recordatorio #{reminder_id} borrado"


if __name__ == "__main__":
    server.run("stdio")
