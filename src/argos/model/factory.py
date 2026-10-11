"""Construcción de proveedores de modelo a partir de la configuración."""

from __future__ import annotations

from argos.config import Config
from argos.model.base import ModelProvider
from argos.model.codex_cli import CodexCliProvider
from argos.model.fake import FakeProvider


def engine_instructions(cfg: Config) -> str | None:
    path = cfg.model.codex.instructions_file
    return (cfg.root / path).read_text(encoding="utf-8") if path else None


def make_provider(
    cfg: Config, name: str | None = None, profile: str | None = None
) -> ModelProvider:
    # `profile` se mantiene por compatibilidad con los llamadores (daemon, CLI); todos los perfiles
    # usan el mismo motor (Codex de la suscripción) salvo el simulado de CI/evals.
    name = name or cfg.model.provider
    if name == "codex":
        c = cfg.model.codex
        return CodexCliProvider(
            cfg.model.name,
            cfg.model.timeout_s,
            overrides=c.overrides,
            disable_features=c.disable_features,
            instructions=engine_instructions(cfg),
        )
    if name == "fake":
        return FakeProvider([])
    raise ValueError(f"provider desconocido: {name}")
