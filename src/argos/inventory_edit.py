"""Escritura del inventario (secrets/inventory.yaml) desde la CLI, fuera del chat/modelo.

Los secretos se introducen por un prompt oculto y se escriben directamente al fichero (modo 600),
sin pasar por el agente ni por la auditoría.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

SECRET_FIELDS = frozenset({"api_key", "apikey", "token", "password", "passwd", "secret",
                           "passphrase", "key", "access_token"})


def inventory_path(root: Path) -> Path:
    return root / "secrets" / "inventory.yaml"


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def set_field(root: Path, service: str, field: str, value: str) -> None:
    path = inventory_path(root)
    data = _load(path)
    services = data.setdefault("services", {})
    if not isinstance(services, dict):
        raise ValueError("inventory.yaml: 'services' debe ser un mapa")
    services.setdefault(service, {})[field] = value
    _save(path, data)


def is_secret(field: str) -> bool:
    return field.lower() in SECRET_FIELDS
