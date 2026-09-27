"""Retención, purga y copias de seguridad (RF-LEG-03, RF-OB-13, RNF-10, RF-LEG-04).

- La retención de una sesión es la de su perfil (`retention_days`) o la global de auditoría.
- Purga = workspace + JSONL + filas de SQLite + blobs que ya nadie referencia. Nunca toca
  sesiones en curso. Lo purgado queda anotado en `purge.jsonl` (sin contenido, solo metadatos).
- Backup = snapshot consistente de SQLite + auditoría JSONL en un .tar.gz cifrado con age.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argos.audit.store import AuditStore
from argos.config import Config
from argos.state import StateStore

_REF = re.compile(r"sha256:([0-9a-f]{64})")


@dataclass
class PurgeReport:
    sessions: list[str] = field(default_factory=list)
    blobs: int = 0
    bytes_freed: int = 0
    memories: int = 0


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def purge(cfg: Config, store: AuditStore, now: datetime | None = None,
          dry_run: bool = False) -> PurgeReport:
    now = now or datetime.now(UTC)
    report = PurgeReport()
    rows = store.db.execute(
        "SELECT id, profile, status, started_at, ended_at FROM sessions").fetchall()
    for sid, profile, status, started, ended in rows:
        if status == "running":
            continue
        prof = cfg.profiles.get(profile)
        days = (prof.retention_days if prof and prof.retention_days is not None
                else cfg.audit.retention_days)
        if datetime.fromisoformat(ended or started) > now - timedelta(days=days):
            continue
        report.sessions.append(sid)
        paths = [cfg.data_path / "workspaces" / sid, store.jsonl_dir / f"{sid}.jsonl"]
        report.bytes_freed += sum(_size(p) for p in paths)
        if dry_run:
            continue
        for p in paths:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        with store._lock:
            store.db.execute("DELETE FROM events WHERE session_id=?", (sid,))
            store.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            store.db.commit()
        with open(cfg.data_path / "purge.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": now.isoformat(), "session_id": sid, "profile": profile,
                                 "retention_days": days}) + "\n")

    # Memoria del agente: solo caduca en perfiles con retención explícita (osint: datos de
    # terceros, RF-LEG-03). La retención de auditoría no aplica: olvidar tu entorno no es
    # minimización, es perder utilidad. Las tuyas solo caducan si les pones fecha.
    retention = {name: p.retention_days for name, p in cfg.profiles.items()
                 if p.retention_days is not None}
    state_db = cfg.data_path / "state.db"
    if state_db.exists() and not dry_run:
        report.memories = StateStore(state_db).purge_memories(retention, now)

    # Recolección de blobs huérfanos: los que ningún evento restante referencia.
    referenced = {m for (data,) in store.db.execute("SELECT data FROM events")
                  for m in _REF.findall(data)}
    for blob in store.blob_dir.glob("*/*"):
        if blob.name not in referenced:
            report.blobs += 1
            report.bytes_freed += blob.stat().st_size
            if not dry_run:
                blob.unlink()
    return report


class BackupError(RuntimeError):
    pass


def age_recipient(cfg: Config) -> str | None:
    """Clave pública age: de .sops.yaml si existe, o derivada de la clave local."""
    sops = cfg.root / ".sops.yaml"
    if sops.exists():
        match = re.search(r"\bage:\s*(age1[0-9a-z]+)", sops.read_text())
        if match:
            return match.group(1)
    return None


def backup(cfg: Config, store: AuditStore, encrypt: bool = True) -> Path:
    recipient = age_recipient(cfg) if encrypt else None
    if encrypt and (recipient is None or shutil.which("age") is None):
        raise BackupError("sin cifrado disponible (falta `age` o .sops.yaml con clave age; "
                          "ejecuta scripts/init-secrets.sh) — usa --no-encrypt bajo tu criterio")
    out_dir = cfg.data_path / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix="argos-backup-") as tmp:
        snapshot = Path(tmp) / "argos.db"
        dst = sqlite3.connect(snapshot)
        with store._lock:
            store.db.backup(dst)   # copia consistente aunque haya escrituras en curso
        dst.close()
        tarball = Path(tmp) / f"argos-{cfg.segment}-{stamp}.tar.gz"
        with tarfile.open(tarball, "w:gz") as tar:
            tar.add(snapshot, arcname="argos.db")
            tar.add(store.jsonl_dir, arcname="audit")
            purge_log = cfg.data_path / "purge.jsonl"
            if purge_log.exists():
                tar.add(purge_log, arcname="purge.jsonl")
        if not encrypt:
            final = out_dir / tarball.name
            shutil.move(tarball, final)
            return final
        final = out_dir / (tarball.name + ".age")
        proc = subprocess.run(["age", "-r", recipient, "-o", str(final), str(tarball)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise BackupError(f"age falló: {proc.stderr.strip()[:300]}")
        return final
