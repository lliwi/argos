"""Proveedor OpenRouter (API compatible con OpenAI, ADR-0028).

Alternativa al motor Codex para perfiles concretos (p. ej. pentest): un modelo vía OpenRouter en
lugar de la suscripción. La clave y el modelo salen del inventario (`open-router`), nunca del
contexto del modelo. El contrato es el mismo que el resto: recibe el contexto y devuelve UNA
decisión; el bucle, las tools y la auditoría siguen siendo de Argos.
"""

from __future__ import annotations

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

API = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_s: int = 300,
        base_url: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = "openrouter"
        self._model = model
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Cabeceras opcionales de atribución que OpenRouter recomienda.
                "HTTP-Referer": "https://github.com/lliwi/argos",
                "X-Title": "Argos",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, request: ModelRequest, route: Route | None = None) -> ModelResponse:
        model = (route.model if route and route.model else None) or self._model
        # El contrato (system + tools + historial + formato de la decisión) va en render(), un
        # único prompt estable entre turnos (favorece caché, RF-CTX-01).
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": request.render()}],
            "temperature": 0,
        }
        start = time.monotonic()
        try:
            resp = await self._http.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ModelError(f"no se pudo contactar con OpenRouter: {exc}") from exc
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code in (401, 403):
            raise ModelAuthError(f"OpenRouter rechazó la api_key (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise ModelError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ModelError(f"respuesta no JSON de OpenRouter: {exc}") from exc
        if err := data.get("error"):
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise ModelError(f"OpenRouter: {msg}")
        choices = data.get("choices") or []
        if not choices:
            raise ModelError(f"OpenRouter sin choices: {str(data)[:200]}")
        text = (choices[0].get("message") or {}).get("content") or ""
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
