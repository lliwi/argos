"""Construcción de proveedores de modelo a partir de la configuración."""

from __future__ import annotations

from argos.config import Config
from argos.model.base import ModelProvider
from argos.model.codex_cli import CodexCliProvider
from argos.model.fake import FakeProvider


def engine_instructions(cfg: Config) -> str | None:
    path = cfg.model.codex.instructions_file
    return (cfg.root / path).read_text(encoding="utf-8") if path else None


def _local_model(cfg: Config) -> ModelProvider | None:
    """Modelo local si el inventario lo tiene `enabled` con provider, url y modelo (ADR-0029).

    La api_key es opcional (Ollama y llama.cpp no suelen pedirla; vLLM con `--api-key` sí).
    """
    from argos.inventory import load_inventory
    from argos.model.local import PROVIDERS, LocalModelProvider

    svc = load_inventory(cfg.root).get("local-model")
    if not svc or not svc.get("enabled"):
        return None
    provider, url, model = svc.get("provider"), svc.get("url"), svc.get("model")
    if provider not in PROVIDERS or not url or not model:
        return None
    key = svc.get("api_key")
    return LocalModelProvider(
        str(provider), str(url), str(model), str(key) if key else None, cfg.model.timeout_s
    )


def make_provider(
    cfg: Config, name: str | None = None, profile: str | None = None
) -> ModelProvider:
    name = name or cfg.model.provider
    # Motor por perfil (ADR-0029): un perfil con engine=local usa el modelo local si está enabled
    # en el inventario; solo sustituye al motor real, no al simulado (CI/evals siguen con fake).
    if (
        name != "fake"
        and profile
        and cfg.profiles.get(profile)
        and (cfg.profiles[profile].engine == "local")
    ):
        if provider := _local_model(cfg):
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
