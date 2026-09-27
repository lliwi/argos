"""Proveedor guionizado y determinista para tests y evaluación sin red (ADR-0001).

Cada paso del guion es una decisión (dict) o texto crudo (para probar el parser). Si el guion se
agota, el proveedor termina con `final` para no colgar el bucle.
"""

from __future__ import annotations

import json
from typing import Any

from argos.model.base import (
    ModelRequest,
    ModelResponse,
    Route,
    Usage,
    estimate_tokens,
    parse_decision,
)


class FakeProvider:
    def __init__(self, script: list[dict[str, Any] | str], name: str = "fake") -> None:
        self.script = list(script)
        self.name = name
        self.requests: list[ModelRequest] = []
        self.routes: list[Route | None] = []

    async def complete(self, request: ModelRequest,
                       route: Route | None = None) -> ModelResponse:
        self.requests.append(request)
        self.routes.append(route)
        prompt = request.render()
        if self.script:
            step = self.script.pop(0)
        else:
            step = {"type": "final", "message": "(guion agotado)"}
        if isinstance(step, dict) and step.get("raise"):
            from argos.model.base import ModelError

            raise ModelError(step["raise"])
        raw = step if isinstance(step, str) else json.dumps(step, ensure_ascii=False)
        usage = Usage(prompt_tokens=estimate_tokens(prompt), completion_tokens=estimate_tokens(raw))
        return ModelResponse(
            decision=parse_decision(raw), usage=usage, latency_ms=0, raw_text=raw,
            model=f"fake/{route.name}" if route else "fake")
