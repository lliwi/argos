"""Cliente del API REST de Home Assistant (autenticación Bearer con token de larga duración).

Operaciones acotadas: leer estados de entidades y llamar a servicios (encender/apagar, etc.).
No se expone ejecución de plantillas ni comandos arbitrarios.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class HomeAssistantError(RuntimeError):
    pass


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30,
        verify: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise HomeAssistantError(f"no se pudo contactar con Home Assistant: {exc}") from exc
        if resp.status_code >= 400:
            raise HomeAssistantError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def ping(self) -> bool:
        try:
            await self._req("GET", "/api/")
            return True
        except HomeAssistantError:
            return False

    async def states(self) -> list[dict]:
        return await self._req("GET", "/api/states") or []

    async def state(self, entity_id: str) -> dict:
        return await self._req("GET", f"/api/states/{quote(entity_id)}")

    async def call_service(self, domain: str, service: str, entity_id: str) -> Any:
        return await self._req(
            "POST", f"/api/services/{quote(domain)}/{quote(service)}", json={"entity_id": entity_id}
        )
