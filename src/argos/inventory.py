"""Inventario de infraestructura (IPs, claves, usuarios, notas) en un fichero local.

Evita tener que pasar estos datos por el chat (que irían al modelo y a la auditoría). Vive en
`secrets/inventory.yaml` (git-ignored, modo 600). Distingue:

- Campos SECRETOS (api_key, token, password…): se registran en el redactor (RF-OB-11), se inyectan
  a las herramientas que los necesitan y **nunca** se muestran al modelo.
- Campos de documentación (url, endpoint, username, notes): el agente puede consultarlos con la
  tool `infra.inventory` para saber qué infraestructura existe.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

SECRET_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "password",
        "passwd",
        "secret",
        "passphrase",
        "key",
        "access_token",
        "community",
    }
)


def _is_secret(field: str) -> bool:
    return field.lower() in SECRET_FIELDS


class Inventory:
    def __init__(self, services: dict[str, dict[str, Any]]) -> None:
        self.services = services

    @classmethod
    def load(cls, path: Path) -> Inventory:
        if not path.exists():
            return cls({})
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            log.warning("inventario %s con permisos %o; recomendado 600", path, mode)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        services = data.get("services") or {}
        if not isinstance(services, dict):
            raise ValueError("inventory.yaml: 'services' debe ser un mapa")
        return cls({str(k): dict(v or {}) for k, v in services.items()})

    def register_secrets(self, redactor) -> None:
        for svc in self.services.values():
            for field, value in svc.items():
                if _is_secret(field) and isinstance(value, str):
                    redactor.register_secret(value)

    def get(self, service: str) -> dict[str, Any]:
        return self.services.get(service, {})

    def secret(self, service: str, field: str) -> str | None:
        value = self.get(service).get(field)
        return str(value) if value is not None else None

    def public_view(self) -> dict[str, dict[str, Any]]:
        """Inventario sin secretos, apto para mostrar al agente."""
        out: dict[str, dict[str, Any]] = {}
        for name, svc in self.services.items():
            out[name] = {k: v for k, v in svc.items() if not _is_secret(k)}
            secretos = [k for k in svc if _is_secret(k)]
            if secretos:
                out[name]["_secretos_configurados"] = sorted(secretos)
        return out


def load_inventory(root: Path, redactor=None) -> Inventory:
    inv = Inventory.load(root / "secrets" / "inventory.yaml")
    if redactor is not None:
        inv.register_secrets(redactor)
    return inv
