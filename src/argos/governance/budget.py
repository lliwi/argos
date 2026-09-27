"""Presupuestos de tokens (RF-GOV-01, RNF-09): warn → pause (pide aprobación) → abort."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from argos.audit.events import BudgetEvent, Event
from argos.audit.store import AuditStore

Action = Literal["ok", "warn", "pause", "abort"]


def tokens_spent_today(store: AuditStore) -> int:
    today = datetime.now(UTC).date().isoformat()
    total = 0
    for (data,) in store.db.execute(
            "SELECT data FROM events WHERE type='turn' AND ts >= ?", (today,)):
        d = json.loads(data)
        total += d.get("prompt_tokens", 0) + d.get("completion_tokens", 0)
    return total


class BudgetTracker:
    def __init__(self, session_id: str, session_limit: int, day_limit: int, warn_ratio: float,
                 day_spent_before: int, emit: Callable[[Event], None]) -> None:
        self.session_id = session_id
        self.session_limit = session_limit
        self.day_limit = day_limit
        self.warn_ratio = warn_ratio
        self.day_before = day_spent_before
        self.spent = 0
        self.emit = emit
        self._warned = False
        self._extended = False

    def check_start(self) -> Action:
        """Antes de arrancar: si el día ya está agotado, no se empieza (P6)."""
        if self.day_before >= self.day_limit:
            self._event("day", self.day_limit, self.day_before, "abort")
            return "abort"
        return "ok"

    def add(self, tokens: int) -> Action:
        self.spent += tokens
        day_total = self.day_before + self.spent
        if day_total >= self.day_limit:
            self._event("day", self.day_limit, day_total, "abort")
            return "abort"
        if self.spent >= self.session_limit:
            if self._extended:
                self._event("session", self.session_limit, self.spent, "abort")
                return "abort"
            self._event("session", self.session_limit, self.spent, "pause")
            return "pause"
        if not self._warned and self.spent >= self.session_limit * self.warn_ratio:
            self._warned = True
            self._event("session", self.session_limit, self.spent, "warn")
            return "warn"
        return "ok"

    def extend(self, factor: float = 1.5) -> None:
        """Tras aprobar una pausa, se amplía el límite una única vez."""
        self.session_limit = int(self.session_limit * factor)
        self._extended = True

    def _event(self, scope: str, limit: float, spent: float, action: str) -> None:
        self.emit(BudgetEvent(session_id=self.session_id, scope=scope, limit=limit, spent=spent,
                              action=action))
