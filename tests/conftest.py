from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from argos.audit.redact import Redactor
from argos.audit.store import AuditStore
from argos.config import ROOT, load_config
from argos.sandbox.docker_sandbox import EgressDecision, ExecResult


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Copia de config/prompts/evals del repo en un directorio aislado."""
    for d in ("config", "prompts", "evals", "skills"):
        shutil.copytree(ROOT / d, tmp_path / d)
    return tmp_path


@pytest.fixture
def cfg(root: Path):
    return load_config(root, {"data_dir": str(root / "var"), "model.provider": "fake"})


@pytest.fixture
def store(cfg) -> AuditStore:
    return AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))


@dataclass
class FakeSandbox:
    """Sandbox en memoria: responde con resultados guionizados por prefijo de comando."""

    responses: dict[str, ExecResult] = field(default_factory=dict)
    blocked: list[EgressDecision] = field(default_factory=list)
    delay_s: float = 0.0
    commands: list[str] = field(default_factory=list)
    destroyed: bool = False
    id: str = "fake-sbx"

    async def exec(self, command: str, timeout_s=None) -> ExecResult:
        self.commands.append(command)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        for prefix, res in self.responses.items():
            if command.startswith(prefix):
                return res
        return ExecResult(0, f"ok: {command}\n", "", 5)

    def egress_blocked_since_last(self) -> list[EgressDecision]:
        out, self.blocked = self.blocked, []
        return out

    async def reset(self) -> None:
        pass

    async def destroy(self) -> None:
        self.destroyed = True


@pytest.fixture
def fake_sandbox() -> FakeSandbox:
    return FakeSandbox()
