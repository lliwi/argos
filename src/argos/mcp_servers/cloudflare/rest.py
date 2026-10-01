"""Cliente del API v4 de Cloudflare (autenticación Bearer con API token).

Operaciones acotadas: zonas, registros DNS, túneles (lectura) y analítica de zona. Sin Workers,
reglas WAF, ajustes de zona ni gestión de tokens/cuenta.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.cloudflare.com/client/v4"
# Códigos de Cloudflare que significan "el token no tiene ese permiso".
_AUTHZ_CODES = {9109, 10000, 10001}


class CloudflareError(RuntimeError):
    pass


class CloudflarePermissionError(CloudflareError):
    pass


class CloudflareClient:
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
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> dict:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"no se pudo contactar con Cloudflare: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400 or body.get("success") is False:
            errors = body.get("errors") or []
            msg = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict))
            codes = {e.get("code") for e in errors if isinstance(e, dict)}
            if resp.status_code == 403 or codes & _AUTHZ_CODES:
                raise CloudflarePermissionError(f"{method} {path}: sin permiso ({msg or 403})")
            detail = msg or resp.text[:300]
            raise CloudflareError(f"{method} {path}: HTTP {resp.status_code} {detail}")
        return body

    async def _paged(self, path: str, params: dict | None = None, max_pages: int = 5) -> list[dict]:
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            body = await self._req(
                "GET", path, params={**(params or {}), "page": page, "per_page": 100}
            )
            out.extend(body.get("result") or [])
            info = body.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                break
        return out

    async def zones(self) -> list[dict]:
        return await self._paged("/zones")

    async def dns_records(self, zone_id: str, rtype: str = "", name: str = "") -> list[dict]:
        params = {k: v for k, v in (("type", rtype), ("name", name)) if v}
        return await self._paged(f"/zones/{zone_id}/dns_records", params)

    async def dns_create(self, zone_id: str, record: dict) -> dict:
        return (await self._req("POST", f"/zones/{zone_id}/dns_records", json=record))["result"]

    async def dns_update(self, zone_id: str, record_id: str, changes: dict) -> dict:
        return (
            await self._req("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json=changes)
        )["result"]

    async def dns_delete(self, zone_id: str, record_id: str) -> None:
        await self._req("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    async def tunnels(self, account_id: str) -> list[dict]:
        return await self._paged(f"/accounts/{account_id}/cfd_tunnel", {"is_deleted": "false"})

    async def zone_analytics(self, zone_id: str, since: str) -> dict:
        query = (
            "query($z: String!, $d: Date!) { viewer { zones(filter: {zoneTag: $z}) { "
            "httpRequests1dGroups(limit: 31, filter: {date_geq: $d}, orderBy: [date_ASC]) { "
            "dimensions { date } sum { requests cachedRequests bytes threats pageViews } "
            "uniq { uniques } } } } }"
        )
        body = await self._req(
            "POST", "/graphql", json={"query": query, "variables": {"z": zone_id, "d": since}}
        )
        if errs := body.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in errs if isinstance(e, dict))
            if any(
                (e.get("extensions") or {}).get("code") == "authz"
                for e in errs
                if isinstance(e, dict)
            ):
                raise CloudflarePermissionError(f"graphql: sin permiso ({msg})")
            raise CloudflareError(f"graphql: {msg}")
        zones = ((body.get("data") or {}).get("viewer") or {}).get("zones") or []
        return zones[0] if zones else {}
