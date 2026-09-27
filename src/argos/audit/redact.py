"""Redacción de secretos y datos personales antes de persistir auditoría (RF-OB-11).

Dos fuentes: (1) registro de valores de secretos conocidos (los carga `argos.secrets`) y
(2) patrones genéricos. Se aplica a todo lo que se escribe en JSONL, SQLite y blobs.
"""

from __future__ import annotations

import re
from typing import Any

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("aws_key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")),
    ("bearer", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{16,}")),
    ("assignment", re.compile(
        r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*)['\"]?[^\s'\"&]{6,}")),
    # Frases libres, también en castellano: "la contraseña es X", "clave: X", "password X".
    # El valor debe parecer un secreto (≥6 caracteres con algún dígito o símbolo) para no
    # redactar texto normal como "la contraseña es segura".
    ("credential", re.compile(
        r"(?i)\b((?:contrase[ñn]a|clave|password|passwd|pwd|pass|passphrase|pin|token|secreto)"
        r"(?:\s+(?:es|is|era|sería|nueva|actual|del?\s+\w+)){0,3}\s*[:=]?\s+)"
        r"['\"]?(?=[^\s'\"]*[\d_\-!@#$%^&*+/.])[^\s'\",;]{6,}")),
]

PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("dni_nie", re.compile(r"\b[XYZ]?\d{7,8}[A-Z]\b")),
    ("phone_es", re.compile(r"(?<!\d)(?:\+34\s?)?[6-9]\d{2}\s?\d{3}\s?\d{3}(?!\d)")),
]


# Campos de identidad/estructura que nunca se redactan (romperían la reconstrucción de trazas).
STRUCTURAL_KEYS = frozenset({
    "id", "type", "ts", "session_id", "parent_session_id", "trace_id", "span_id",
    "parent_span_id", "turn_id", "retry_of", "hash", "config_hash", "harness_commit",
    "prompt_version", "decided_at",
})


class Redactor:
    def __init__(self, redact_pii: bool = True) -> None:
        self.redact_pii = redact_pii
        self._known: set[str] = set()

    def register_secret(self, value: str) -> None:
        # Valores muy cortos darían falsos positivos masivos; se exige una longitud mínima.
        if value and len(value) >= 6:
            self._known.add(value)

    def redact_secrets(self, text: str) -> str:
        """Solo secretos (conocidos + patrones), sin datos personales. Se aplica a lo que entra
        en el contexto del modelo (RNF-06): OSINT necesita ver datos personales, nunca secretos."""
        return self._secrets(text)

    def redact_text(self, text: str) -> str:
        text = self._secrets(text)
        if self.redact_pii:
            for name, pat in PII_PATTERNS:
                text = pat.sub(f"[REDACTED:{name}]", text)
        return text

    def _secrets(self, text: str) -> str:
        for value in sorted(self._known, key=len, reverse=True):
            text = text.replace(value, "[REDACTED:secret]")
        for name, pat in SECRET_PATTERNS:
            if name in ("bearer", "assignment", "credential"):
                text = pat.sub(lambda m, n=name: f"{m.group(1)}[REDACTED:{n}]", text)
            else:
                text = pat.sub(f"[REDACTED:{name}]", text)
        return text

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                k: v if k in STRUCTURAL_KEYS or k.endswith("_ref") else self.redact(v)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        return value
