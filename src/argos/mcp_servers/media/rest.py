"""Clientes REST de Jackett (búsqueda en indexadores) y Transmission (cliente de descargas).

Jackett: API v2.0 (apikey por query). Transmission: RPC con el handshake de 409 →
X-Transmission-Session-Id. Operaciones acotadas; sin ejecución de comandos.
"""

from __future__ import annotations

from typing import Any

import httpx

# Categorías Torznab habituales (nombre amigable → id).
CATEGORIES = {"movies": 2000, "music": 3000, "audio": 3000, "tv": 5000, "series": 5000,
              "books": 7000, "book": 7000, "other": 8000}


class MediaError(RuntimeError):
    pass


class JackettClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport,
                                       follow_redirects=True)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def search(self, query: str, category: str = "") -> list[dict]:
        params: list[tuple[str, str]] = [("apikey", self._key), ("Query", query)]
        cat = CATEGORIES.get(category.lower().strip()) if category else None
        if cat:
            params.append(("Category[]", str(cat)))
        try:
            r = await self._http.get(f"{self._base}/api/v2.0/indexers/all/results", params=params)
        except httpx.HTTPError as exc:
            raise MediaError(f"no se pudo contactar con Jackett: {exc}") from exc
        if r.status_code >= 400:
            raise MediaError(f"Jackett HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json().get("Results", []) or []
        except ValueError as exc:
            raise MediaError(f"Jackett devolvió una respuesta no JSON: {exc}") from exc


class TransmissionClient:
    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 30,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = base_url.rstrip("/") + "/transmission/rpc"
        auth = (username, password) if username else None
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport, auth=auth)
        self._session_id = ""

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _rpc(self, method: str, arguments: dict | None = None) -> dict:
        payload = {"method": method, "arguments": arguments or {}}
        for _ in range(2):
            try:
                r = await self._http.post(self._url, json=payload,
                                          headers={"X-Transmission-Session-Id": self._session_id})
            except httpx.HTTPError as exc:
                raise MediaError(f"no se pudo contactar con Transmission: {exc}") from exc
            if r.status_code == 409:   # handshake: guarda el session id y reintenta
                self._session_id = r.headers.get("X-Transmission-Session-Id", "")
                continue
            if r.status_code >= 400:
                raise MediaError(f"Transmission HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if data.get("result") != "success":
                raise MediaError(f"Transmission: {data.get('result')}")
            return data.get("arguments", {})
        raise MediaError("Transmission: handshake de sesión fallido")

    async def torrents(self) -> list[dict]:
        args = await self._rpc("torrent-get", {"fields": [
            "id", "name", "percentDone", "status", "rateDownload", "totalSize", "eta"]})
        return args.get("torrents", [])

    async def add(self, link: str) -> dict[str, Any]:
        args = await self._rpc("torrent-add", {"filename": link})
        return args.get("torrent-added") or args.get("torrent-duplicate") or {}

    async def action(self, torrent_id: int, action: str, delete_data: bool = False) -> None:
        if action == "remove":
            await self._rpc("torrent-remove", {"ids": [torrent_id],
                                               "delete-local-data": delete_data})
        else:
            await self._rpc(f"torrent-{action}", {"ids": [torrent_id]})  # start | stop
