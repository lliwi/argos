"""Retención y backups (RF-LEG-03, RNF-10)."""

from __future__ import annotations

import tarfile
from datetime import UTC, datetime, timedelta

from argos import retention
from argos.core.session import SessionOptions, run_session
from argos.model.fake import FakeProvider
from argos.sandbox.docker_sandbox import ExecResult


async def _session(cfg, store, sandbox, output="x" * 50):
    sandbox.responses["cat"] = ExecResult(0, output, "", 1)
    return await run_session(
        SessionOptions(task="t"),
        cfg,
        FakeProvider(
            [
                {"type": "tool_call", "tool": "shell.exec", "args": {"command": "cat f"}},
                {"type": "final", "message": "ok"},
            ]
        ),
        store=store,
        sandbox_factory=lambda: sandbox,
    )


async def test_purge_respects_retention_and_collects_blobs(cfg, store, fake_sandbox):
    old = await _session(cfg, store, fake_sandbox, "salida-antigua")
    new = await _session(cfg, store, fake_sandbox, "salida-nueva")
    future = datetime.now(UTC) + timedelta(days=cfg.audit.retention_days + 1)
    # Solo la sesión "antigua" queda fuera de plazo: la nueva se marca como recién terminada.
    store.db.execute(
        "UPDATE sessions SET ended_at=? WHERE id=?",
        ((future - timedelta(hours=1)).isoformat(), new.session_id),
    )
    store.db.commit()

    dry = retention.purge(cfg, store, now=future, dry_run=True)
    assert dry.sessions == [old.session_id] and old.workspace.exists()

    report = retention.purge(cfg, store, now=future)
    assert report.sessions == [old.session_id] and report.blobs >= 1
    assert not old.workspace.exists() and new.workspace.exists()
    assert store.events(old.session_id) == [] and store.events(new.session_id)
    assert not (store.jsonl_dir / f"{old.session_id}.jsonl").exists()
    assert old.session_id in (cfg.data_path / "purge.jsonl").read_text()
    remaining = {b.name for b in store.blob_dir.glob("*/*")}
    call = [e for e in store.events(new.session_id, ["tool_call"])][0]
    assert call.result_ref.removeprefix("sha256:") in remaining


async def test_backup_unencrypted_contains_db_and_audit(cfg, store, fake_sandbox):
    await _session(cfg, store, fake_sandbox)
    path = retention.backup(cfg, store, encrypt=False)
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert "argos.db" in names and any(n.startswith("audit/") for n in names)


def test_backup_refuses_without_encryption_key(cfg, store):
    import pytest

    with pytest.raises(retention.BackupError, match="sin cifrado"):
        retention.backup(cfg, store, encrypt=True)


def test_memory_purge_only_for_profiles_with_explicit_retention(cfg, store):
    from argos.state import StateStore

    st = StateStore(cfg.data_path / "state.db")
    keep = st.add_memory("personal", "fact", "el NAS está en 192.168.1.10", "agent")
    gone = st.add_memory("osint", "finding", "cuenta encontrada en foro", "agent")
    future = datetime.now(UTC) + timedelta(days=cfg.audit.retention_days + 1)
    report = retention.purge(cfg, store, now=future)
    ids = {m.id for m in st.memories()}
    assert report.memories == 1 and keep.id in ids and gone.id not in ids
