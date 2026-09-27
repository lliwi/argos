"""Cliente de la API del núcleo persistente (socket Unix). Lo usan la CLI y los canales."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx


class CoreUnavailable(RuntimeError):
    pass


class CoreClient:
    def __init__(self, socket_path: Path, timeout: float = 30) -> None:
        if not socket_path.exists():
            raise CoreUnavailable(f"el núcleo no está en marcha (no existe {socket_path}); "
                                  "arráncalo con `argos serve`")
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket_path)),
            base_url="http://argos", timeout=timeout)

    async def __aenter__(self) -> CoreClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.TransportError as exc:
            raise CoreUnavailable(f"no se pudo contactar con el núcleo: {exc}") from exc
        data = resp.json()
        if resp.status_code >= 400:
            raise RuntimeError(data.get("error", resp.text))
        return data

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def state(self) -> dict[str, Any]:
        return await self._call("GET", "/state")

    async def submit(self, **body: Any) -> str:
        return (await self._call("POST", "/sessions", json=body))["session_id"]

    async def create_thread(self, title: str, profile: str, channel: str) -> dict[str, Any]:
        return await self._call("POST", "/threads", json={"title": title, "profile": profile,
                                                          "channel": channel})

    async def threads(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/threads")

    async def tools(self, profile: str = "personal") -> list[dict[str, Any]]:
        return await self._call("GET", "/tools", params={"profile": profile})

    async def memories(self, profile: str | None = None) -> list[dict[str, Any]]:
        return await self._call("GET", "/memory", params={"profile": profile} if profile else {})

    async def kill(self, reason: str) -> None:
        await self._call("POST", "/kill", json={"reason": reason})

    async def rearm(self) -> None:
        await self._call("POST", "/rearm")

    async def sessions(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/sessions")

    async def cancel(self, sid: str) -> bool:
        return (await self._call("POST", f"/sessions/{sid}/cancel"))["cancelled"]

    async def approvals(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/approvals")

    async def decide(self, approval_id: str, decision: str, approver: str,
                     channel: str) -> bool:
        return (await self._call("POST", f"/approvals/{approval_id}", json={
            "decision": decision, "approver": approver, "channel": channel}))["ok"]

    async def schedules(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/schedules")

    async def run_schedule(self, name: str) -> dict[str, Any]:
        return await self._call("POST", f"/schedules/{name}/run")

    async def events_all(self) -> AsyncIterator[dict[str, Any]]:
        """Flujo global: aprobaciones pendientes y luego todos los eventos en vivo."""
        async with self._client.stream("GET", "/events", timeout=None) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])

    async def events(self, sid: str, replay: bool = True) -> AsyncIterator[dict[str, Any]]:
        """Eventos de la sesión (y sus subagentes) en vivo; termina con su `session_end`."""
        params = {"replay": "1" if replay else "0"}
        async with self._client.stream("GET", f"/sessions/{sid}/events", params=params,
                                       timeout=None) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
