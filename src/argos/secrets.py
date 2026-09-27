"""Gestor de secretos con SOPS + age (ADR-0005, RF-SEC-04).

Los secretos se descifran en memoria, se filtran por perfil (RF-SEC-01), se registran en el
redactor de auditoría (RF-OB-11) y solo se entregan como entorno a sandbox/tools. Este módulo no
ofrece ninguna vía para serializarlos al contexto del modelo.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

import yaml

from argos.audit.redact import Redactor
from argos.config import Profile

log = logging.getLogger(__name__)


class SecretsError(RuntimeError):
    pass


def load_profile_secrets(root: Path, profile: Profile, redactor: Redactor) -> dict[str, str]:
    """Devuelve solo los secretos declarados por el perfil. Faltantes => aviso, no error."""
    if not profile.secrets:
        return {}
    path = root / "secrets" / f"{profile.name}.sops.yaml"
    if not path.exists():
        log.warning("perfil %s declara secretos pero no existe %s", profile.name, path)
        return {}
    if shutil.which("sops") is None:
        log.warning("sops no instalado: el perfil %s arranca sin secretos", profile.name)
        return {}
    proc = subprocess.run(
        ["sops", "--decrypt", "--output-type", "yaml", str(path)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        # No se incluye stdout: podría contener material descifrado parcial.
        raise SecretsError(f"sops no pudo descifrar {path.name}: {proc.stderr.strip()[:300]}")
    data = yaml.safe_load(proc.stdout) or {}
    out: dict[str, str] = {}
    for ref in profile.secrets:
        if ref.name in data:
            value = str(data[ref.name])
            redactor.register_secret(value)
            out[ref.name] = value
        else:
            log.warning("secreto %s ausente en %s", ref.name, path.name)
    return out
