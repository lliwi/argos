"""Proveedor de modelo local: Ollama, llama.cpp o vLLM (ADR-0029).

Motor opcional por perfil (p. ej. pentest): un modelo servido en la red propia en lugar de la
suscripción. Los tres servidores exponen la API compatible con OpenAI (`/v1/chat/completions`),
así que basta un único cliente. URL, modelo y api_key (opcional) salen del inventario
(`local-model`), nunca del contexto del modelo. El contrato es el mismo que el resto: recibe el
contexto y devuelve UNA decisión; el bucle, las tools y la auditoría siguen siendo de Argos.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from argos.model.base import (
    ModelAuthError,
    ModelError,
    ModelRequest,
    ModelResponse,
    Route,
    Usage,
    estimate_tokens,
    parse_decision,
)

PROVIDERS = ("ollama", "llama.cpp", "vllm")

# Modelos con razonamiento (qwen3, deepseek-r1…) pueden devolver el pensamiento en el contenido.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def api_base(url: str) -> str:
    """Base `/v1` de la API compatible con OpenAI, admita o no la URL el sufijo."""
    url = url.rstrip("/")
    return url if url.endswith("/v1") else f"{url}/v1"


class LocalModelProvider:
    def __init__(
        self,
        provider: str,
        url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"provider local desconocido: {provider} (usa {', '.join(PROVIDERS)})")
        self.name = "local"
        self._label = provider
        self._model = model
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=api_base(url), timeout=timeout_s, transport=transport, headers=headers
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, request: ModelRequest, route: Route | None = None) -> ModelResponse:
        # Se ignora `route.model`: las rutas (decide/hard/internal) nombran modelos de Codex, que no
        # existen en el servidor local. El servidor sirve un único modelo, el del inventario.
        model = self._model
        # El contrato (system + tools + historial + formato de la decisión) va en render(), un
        # único prompt estable entre turnos (favorece caché de prefijo, RF-CTX-01).
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": request.render()}],
            "temperature": 0,
        }
        who = f"modelo local ({self._label})"
        start = time.monotonic()
        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelError(f"no se pudo contactar con el {who}: {exc}") from exc
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code in (401, 403):
            raise ModelAuthError(f"el {who} rechazó la api_key (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ModelError(f"{who} HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelError(f"respuesta no JSON del {who}: {exc}") from exc
        if err := data.get("error"):
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise ModelError(f"{who}: {msg}")
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"{who} sin choices: {str(data)[:200]}")
        text = _THINK.sub("", (choices[0].get("message") or {}).get("content") or "").strip()
        u = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(u.get("prompt_tokens", 0) or estimate_tokens(request.render())),
            completion_tokens=int(u.get("completion_tokens", 0) or estimate_tokens(text)),
            cached_tokens=int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0),
        )
        return ModelResponse(
            decision=parse_decision(text),
            usage=usage,
            latency_ms=latency,
            raw_text=text,
            model=data.get("model") or model,
        )
