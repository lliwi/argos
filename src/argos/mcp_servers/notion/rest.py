"""Cliente del API de Notion (token de integración interna, Bearer).

Solo lo que usa el MCP: búsqueda, páginas, bloques y bases de datos. La integración solo ve lo
que se ha compartido con ella en Notion (ese es su alcance real).
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self,
        token: str,
        base_url: str = API,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": VERSION,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> dict:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise NotionError(f"no se pudo contactar con Notion: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            code, msg = body.get("code", ""), body.get("message", resp.text[:300])
            if resp.status_code == 404:
                msg += " (¿está la página compartida con la integración?)"
            if resp.status_code == 403:
                msg += " (a la integración le falta esa capacidad en Notion)"
            raise NotionError(f"{method} {path}: HTTP {resp.status_code} {code}: {msg}")
        return body

    async def search(self, query: str, kind: str = "", limit: int = 20) -> list[dict]:
        body: dict[str, Any] = {
            "query": query,
            "page_size": min(limit, 100),
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        }
        if kind:
            body["filter"] = {"property": "object", "value": kind}
        return (await self._req("POST", "/search", json=body)).get("results", [])

    async def page(self, page_id: str) -> dict:
        return await self._req("GET", f"/pages/{page_id}")

    async def database(self, database_id: str) -> dict:
        return await self._req("GET", f"/databases/{database_id}")

    async def children(self, block_id: str, max_items: int = 300) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while len(out) < max_items:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            body = await self._req("GET", f"/blocks/{block_id}/children", params=params)
            out.extend(body.get("results", []))
            if not body.get("has_more"):
                break
            cursor = body.get("next_cursor")
        return out[:max_items]

    async def query(
        self, database_id: str, filter_: dict | None, sorts: list | None, limit: int
    ) -> list[dict]:
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            body: dict[str, Any] = {"page_size": min(100, limit - len(out))}
            if filter_:
                body["filter"] = filter_
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            res = await self._req("POST", f"/databases/{database_id}/query", json=body)
            out.extend(res.get("results", []))
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return out

    async def create_page(self, parent: dict, properties: dict, children: list[dict]) -> dict:
        page = await self._req(
            "POST",
            "/pages",
            json={"parent": parent, "properties": properties, "children": children[:100]},
        )
        if len(children) > 100:
            await self.append(page["id"], children[100:])
        return page

    async def append(self, block_id: str, children: list[dict]) -> None:
        for i in range(0, len(children), 100):
            await self._req(
                "PATCH", f"/blocks/{block_id}/children", json={"children": children[i : i + 100]}
            )

    async def update_page(self, page_id: str, body: dict) -> dict:
        return await self._req("PATCH", f"/pages/{page_id}", json=body)
