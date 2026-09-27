"""Validación de peticiones al broker de sandbox (ADR-0008).

El núcleo solo puede pedir: crear/usar/destruir el sandbox de *su* sesión, dominios extra de
egress y variables de entorno. Todo lo demás (imagen, red, proxy, límites, rutas montadas) lo
decide el broker con su propia configuración.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
DOMAIN_RE = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
RESERVED_ENV = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY",
                          "no_proxy", "PATH", "HOME"})
MAX_DOMAINS = 20
MAX_TIMEOUT_S = 600
MAX_COMMAND = 20_000


class PolicyError(ValueError):
    pass


def session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_RE.match(value):
        raise PolicyError("session_id inválido")
    return value


def workspace(segment_root: Path, sid: str) -> Path:
    """El único workspace que se puede montar para esa sesión en ese segmento."""
    path = segment_root / "workspaces" / sid
    if not (path / "in").is_dir() or not (path / "out").is_dir():
        raise PolicyError("el workspace de la sesión no existe")
    return path


def domains(value: Any) -> list[str]:
    value = value or []
    if not isinstance(value, list) or len(value) > MAX_DOMAINS:
        raise PolicyError(f"allow_extra: lista de hasta {MAX_DOMAINS} dominios")
    out = []
    for item in value:
        if not isinstance(item, str) or not DOMAIN_RE.match(item.lower()):
            raise PolicyError(f"dominio inválido: {item!r}")
        out.append(item.lower())
    return out


def env(value: Any) -> dict[str, str]:
    value = value or {}
    if not isinstance(value, dict):
        raise PolicyError("env debe ser un objeto")
    for key, val in value.items():
        if not isinstance(key, str) or not ENV_KEY_RE.match(key) or not isinstance(val, str):
            raise PolicyError(f"variable de entorno inválida: {key!r}")
        if key in RESERVED_ENV:
            raise PolicyError(f"variable reservada: {key}")
    return value


def command(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_COMMAND:
        raise PolicyError("command inválido")
    return value


def timeout(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or not 1 <= value <= MAX_TIMEOUT_S:
        raise PolicyError(f"timeout_s entre 1 y {MAX_TIMEOUT_S}")
    return value
