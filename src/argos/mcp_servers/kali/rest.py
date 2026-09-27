"""Cliente del API REST de mcp-kali-server (contenedor `kali`).

Se usan los endpoints por herramienta (`/api/tools/<tool>`) y, solo para gestión de paquetes,
`/api/command` con comandos `apt-get` que construye este cliente a partir de nombres de paquete
ya validados. No existe un método de comando libre: nunca se envía texto arbitrario del agente.
"""

from __future__ import annotations

import httpx


class KaliError(RuntimeError):
    pass


class KaliRestClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 600) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout,
                                       headers=headers)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> bool:
        try:
            resp = await self._http.get("/health", timeout=5)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def run_tool(self, tool: str, payload: dict) -> str:
        return await self._post(f"/api/tools/{tool}", payload)

    async def apt_update(self) -> str:
        return await self._post("/api/command", {"command": "apt-get update"})

    async def apt_install(self, packages: list[str]) -> str:
        # `packages` ya viene validado (nombres de paquete apt); el comando lo compone el cliente.
        joined = " ".join(packages)
        cmd = f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {joined}"
        return await self._post("/api/command", {"command": cmd})

    async def _post(self, path: str, payload: dict) -> str:
        try:
            resp = await self._http.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise KaliError(f"no se pudo contactar con el servidor Kali: {exc}") from exc
        if resp.status_code >= 400:
            raise KaliError(f"kali {path}: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError:
            return resp.text
        # El servidor devuelve la salida bajo alguna de estas claves según versión.
        for key in ("stdout", "output", "result", "results"):
            if isinstance(data.get(key), str):
                out = data[key]
                if isinstance(data.get("stderr"), str) and data["stderr"].strip():
                    out += f"\n--- stderr ---\n{data['stderr']}"
                return out
        return str(data)
