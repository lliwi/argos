"""Construcción de proveedores de modelo a partir de la configuración."""

from __future__ import annotations

from argos.config import Config
from argos.model.base import ModelProvider
from argos.model.codex_cli import CodexCliProvider
from argos.model.fake import FakeProvider


def engine_instructions(cfg: Config) -> str | None:
    path = cfg.model.codex.instructions_file
    return (cfg.root / path).read_text(encoding="utf-8") if path else None


def _openrouter(cfg: Config) -> ModelProvider | None:
    """Proveedor OpenRouter si el inventario lo tiene `enabled` con api_key y modelo (ADR-0028)."""
    from argos.inventory import load_inventory
    from argos.model.openrouter import OpenRouterProvider

    svc = load_inventory(cfg.root).get("open-router")
    if not svc or not svc.get("enabled"):
        return None
    key, model = svc.get("api_key"), svc.get("model")
    if not key or not model:
        return None
    return OpenRouterProvider(str(key), str(model), cfg.model.timeout_s)


def make_provider(
    cfg: Config, name: str | None = None, profile: str | None = None
) -> ModelProvider:
    name = name or cfg.model.provider
    # Motor por perfil (ADR-0028): un perfil con engine=openrouter usa OpenRouter si está enabled
    # en el inventario; solo sustituye al motor real, no al simulado (CI/evals siguen con fake).
    if (
        name != "fake"
        and profile
        and cfg.profiles.get(profile)
        and (cfg.profiles[profile].engine == "openrouter")
    ):
        if provider := _openrouter(cfg):
            return provider
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
