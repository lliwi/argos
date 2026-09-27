"""Kill switch global (RF-GOV-02): detiene todas las sesiones y bloquea nuevas hasta rearme.

Dos disparadores equivalentes: flag en SQLite (`argos kill`) o fichero `var/KILL` (útil desde
fuera del proceso, p. ej. `touch var/KILL`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from argos.audit.store import AuditStore

KEY = "killswitch"


class KillSwitch:
    def __init__(self, store: AuditStore) -> None:
        self.store = store
        self.file = store.root / "KILL"

    def engage(self, reason: str = "manual") -> None:
        self.store.set_control(KEY, json.dumps(
            {"active": True, "reason": reason, "ts": datetime.now(UTC).isoformat()}))
        self.file.write_text(reason + "\n")

    def rearm(self) -> None:
        self.store.set_control(KEY, json.dumps({"active": False}))
        self.file.unlink(missing_ok=True)

    def active(self) -> str | None:
        """Devuelve el motivo si está activo, None si no."""
        if self.file.exists():
            return self.file.read_text().strip() or "fichero KILL presente"
        raw = self.store.get_control(KEY)
        if raw:
            data = json.loads(raw)
            if data.get("active"):
                return data.get("reason", "activo")
        return None
