"""Cliente del API de Portainer (autenticación por cabecera X-API-Key).

Se usan el API propio de Portainer (`/api/endpoints`, `/api/stacks`) y su proxy al API de Docker
(`/api/endpoints/{id}/docker/...`) para contenedores. Operaciones acotadas: listar/inspeccionar,
logs, y acciones de ciclo de vida (start/stop/restart). No se expone ejecución de comandos.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class PortainerError(RuntimeError):
    pass


class PortainerClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        endpoint: int = 1,
        timeout: float = 30,
        verify: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={"X-API-Key": api_key},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise PortainerError(f"no se pudo contactar con Portainer: {exc}") from exc
        if resp.status_code >= 400:
            raise PortainerError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _docker(self, path: str) -> str:
        return f"/api/endpoints/{self.endpoint}/docker{path}"

    async def ping(self) -> bool:
        try:
            await self._req("GET", "/api/status")
            return True
        except PortainerError:
            return False

    async def endpoints(self) -> list[dict]:
        return await self._req("GET", "/api/endpoints") or []

    async def containers(self, all_: bool = True) -> list[dict]:
        return (
            await self._req(
                "GET", self._docker("/containers/json"), params={"all": "1" if all_ else "0"}
            )
            or []
        )

    async def inspect(self, container_id: str) -> dict:
        return await self._req("GET", self._docker(f"/containers/{quote(container_id)}/json"))

    async def logs(self, container_id: str, tail: int = 200) -> str:
        out = await self._req(
            "GET",
            self._docker(f"/containers/{quote(container_id)}/logs"),
            params={"stdout": "1", "stderr": "1", "tail": str(tail)},
        )
        return out if isinstance(out, str) else str(out)

    async def container_action(self, container_id: str, action: str) -> None:
        # action ∈ {start, stop, restart}
        await self._req("POST", self._docker(f"/containers/{quote(container_id)}/{action}"))

    async def stacks(self) -> list[dict]:
        return await self._req("GET", "/api/stacks") or []
