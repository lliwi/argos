"""Cliente del API del servidor OSINT propio (cabecera X-OSINT-API-Key).

Operaciones acotadas: catálogo de herramientas, lanzar un workflow, esperar su resultado e
informe en markdown. Sin `/tools/import` (registra binarios en el servidor) ni subida de ficheros.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

TERMINAL = {"completed", "failed", "error", "cancelled"}


class OsintError(RuntimeError):
    pass


class OsintClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 45,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"X-OSINT-API-Key": api_key},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise OsintError(f"no se pudo contactar con el servidor OSINT: {exc}") from exc
        if resp.status_code in (401, 403):
            raise OsintError(f"{method} {path}: api_key rechazada (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            raise OsintError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp

    async def tools(self, category: str = "") -> list[dict]:
        params = {"category": category} if category else {}
        return (await self._req("GET", "/tools", params=params)).json()

    async def run(self, request: dict) -> str:
        body = (await self._req("POST", "/workflow/run", json=request)).json()
        if not body.get("task_id"):
            raise OsintError(f"respuesta sin task_id: {str(body)[:200]}")
        return body["task_id"]

    async def task(self, task_id: str, wait: int = 20) -> dict:
        return (await self._req("GET", f"/tasks/{task_id}", params={"wait": wait})).json()

    async def wait(self, task_id: str, deadline_s: float = 480) -> dict:
        """Espera a que la tarea termine (long-poll de hasta 20 s por petición)."""
        end = time.monotonic() + deadline_s
        while True:
            st = await self.task(task_id)
            if st.get("status") in TERMINAL:
                return st
            if time.monotonic() >= end:
                return st
            await asyncio.sleep(0.5)

    async def report(self, task_id: str) -> str:
        return (await self._req("POST", f"/reports/{task_id}", json={"format": "markdown"})).text
