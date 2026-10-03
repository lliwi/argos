"""Proveedor de modelo local y selección de motor por perfil (ADR-0029)."""

from __future__ import annotations

import json

import httpx
import pytest

from argos.model.base import Message, ModelAuthError, ModelError, ModelRequest
from argos.model.local import LocalModelProvider, api_base


def _req():
    return ModelRequest(system="eres argos", tools=[], messages=[Message("user", "hola")])


def _provider(handler, api_key=None, provider="ollama"):
    return LocalModelProvider(
        provider,
        "http://modelo.local:11434",
        "qwen3:8b",
        api_key,
        transport=httpx.MockTransport(handler),
    )


def _final(text="listo"):
    return json.dumps({"type": "final", "tool": "", "args_json": "", "message": text})


async def test_completion_parses_decision_and_usage():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        captured["auth"] = req.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "model": "qwen3:8b",
                "choices": [{"message": {"content": _final()}}],
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
    assert captured["url"] == "http://modelo.local:11434/v1/chat/completions"
    assert captured["auth"] is None  # sin api_key no se envía Authorization
    assert captured["body"]["model"] == "qwen3:8b"
    assert captured["body"]["messages"][0]["role"] == "user"  # render() completo como prompt


async def test_api_key_is_optional_bearer_and_think_is_stripped():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["auth"] = req.headers.get("authorization")
        content = '<think>{"type": "tool_call"}</think>\n' + _final("ok")
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    p = _provider(handler, api_key="k", provider="vllm")
    try:
        r = await p.complete(_req())
    finally:
        await p.aclose()
    assert captured["auth"] == "Bearer k"
    assert r.decision.type == "final" and r.decision.message == "ok"
    assert r.usage.prompt_tokens > 0  # sin usage del servidor => estimación


def test_api_base_and_unknown_provider():
    assert api_base("http://h:8000") == "http://h:8000/v1"
    assert api_base("http://h:8000/v1/") == "http://h:8000/v1"
    with pytest.raises(ValueError):
        LocalModelProvider("openrouter", "http://h", "m")


async def test_auth_and_api_errors():
    p = _provider(lambda req: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(ModelAuthError):
        await p.complete(_req())
    await p.aclose()
    p = _provider(lambda req: httpx.Response(200, json={"error": {"message": "model not found"}}))
    with pytest.raises(ModelError, match="model not found"):
        await p.complete(_req())
    await p.aclose()


def _inventory(root, body):
    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "inventory.yaml").write_text(body, encoding="utf-8")


def test_pentest_uses_local_model_when_enabled(root, monkeypatch):
    from argos.config import load_config
    from argos.model import factory

    _inventory(
        root,
        "services:\n  local-model:\n    provider: llama.cpp\n    url: http://h:8080\n"
        "    model: qwen3\n    enabled: true\n",  # sin api_key: opcional
    )
    cfg = load_config(root, {"data_dir": str(root / "var")})
    # pentest con engine=local y motor real => modelo local; otros perfiles => codex.
    assert cfg.profile("pentest").engine == "local"
    monkeypatch.setattr(factory, "CodexCliProvider", lambda *a, **k: _tag("codex"))
    assert factory.make_provider(cfg, "codex", "pentest").name == "local"
    assert factory.make_provider(cfg, "codex", "orchestrator").name == "codex"
    # Con el motor simulado (CI/evals) nunca se sustituye.
    assert factory.make_provider(cfg, "fake", "pentest").name == "fake"


@pytest.mark.parametrize(
    "svc",
    [
        "    provider: ollama\n    url: http://h\n    model: m\n    enabled: false\n",
        "    provider: openrouter\n    url: http://h\n    model: m\n    enabled: true\n",
        "    provider: ollama\n    model: m\n    enabled: true\n",  # sin url
    ],
)
def test_disabled_or_unconfigured_falls_back(root, monkeypatch, svc):
    from argos.config import load_config
    from argos.model import factory

    _inventory(root, "services:\n  local-model:\n" + svc)
    cfg = load_config(root, {"data_dir": str(root / "var")})
    monkeypatch.setattr(factory, "CodexCliProvider", lambda *a, **k: _tag("codex"))
    assert factory.make_provider(cfg, "codex", "pentest").name == "codex"


class _Tag:
    def __init__(self, name):
        self.name = name


def _tag(name):
    return _Tag(name)
