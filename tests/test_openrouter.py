"""Proveedor OpenRouter y selección de motor por perfil (ADR-0028)."""

from __future__ import annotations

import json

import httpx
import pytest

from argos.model.base import Message, ModelAuthError, ModelError, ModelRequest
from argos.model.openrouter import OpenRouterProvider


def _req():
    return ModelRequest(system="eres argos", tools=[], messages=[Message("user", "hola")])


def _provider(handler):
    return OpenRouterProvider("k", "z-ai/glm-5.3-flash", transport=httpx.MockTransport(handler))


async def test_completion_parses_decision_and_usage():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        content = json.dumps({"type": "final", "tool": "", "args_json": "", "message": "listo"})
        return httpx.Response(
            200,
            json={
                "model": "z-ai/glm-5.3-flash",
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 3},
            },
        )

    p = _provider(handler)
    try:
        r = await p.complete(_req())
    finally:
        await p.aclose()
    assert r.decision.type == "final" and r.decision.message == "listo"
    assert r.usage.prompt_tokens == 11 and r.usage.completion_tokens == 3
    assert captured["auth"] == "Bearer k"
    assert captured["body"]["model"] == "z-ai/glm-5.3-flash"
    assert captured["body"]["messages"][0]["role"] == "user"  # render() completo como prompt


async def test_auth_and_api_errors():
    p = _provider(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ModelAuthError):
        await p.complete(_req())
    await p.aclose()
    p = _provider(lambda req: httpx.Response(200, json={"error": {"message": "rate limited"}}))
    with pytest.raises(ModelError, match="rate limited"):
        await p.complete(_req())
    await p.aclose()


def test_pentest_uses_openrouter_when_enabled(root, monkeypatch):
    from argos.config import load_config
    from argos.model import factory

    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "inventory.yaml").write_text(
        "services:\n  open-router:\n    api_key: or-key\n    model: z-ai/glm-5.3-flash\n"
        "    enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(root, {"data_dir": str(root / "var")})
    # pentest con engine=openrouter y motor real => OpenRouter; otros perfiles => codex.
    assert cfg.profile("pentest").engine == "openrouter"
    monkeypatch.setattr(factory, "CodexCliProvider", lambda *a, **k: _tag("codex"))
    assert factory.make_provider(cfg, "codex", "pentest").name == "openrouter"
    assert factory.make_provider(cfg, "codex", "orchestrator").name == "codex"
    # Con el motor simulado (CI/evals) nunca se sustituye.
    assert factory.make_provider(cfg, "fake", "pentest").name == "fake"


def test_disabled_or_unconfigured_falls_back(root, monkeypatch):
    from argos.config import load_config
    from argos.model import factory

    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "inventory.yaml").write_text(
        "services:\n  open-router:\n    api_key: or-key\n    model: m\n    enabled: false\n",
        encoding="utf-8",
    )
    cfg = load_config(root, {"data_dir": str(root / "var")})
    monkeypatch.setattr(factory, "CodexCliProvider", lambda *a, **k: _tag("codex"))
    assert factory.make_provider(cfg, "codex", "pentest").name == "codex"  # enabled:false


class _Tag:
    def __init__(self, name):
        self.name = name


def _tag(name):
    return _Tag(name)
