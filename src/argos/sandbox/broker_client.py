"""Cliente del broker de sandbox: misma interfaz que DockerSandbox, sin tocar Docker."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from argos.sandbox.docker_sandbox import EgressDecision, ExecResult, SandboxError


class BrokerSandbox:
    def __init__(
        self,
        socket_path: Path,
        session_id: str,
        allow_extra: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.session_id = session_id
        self.allow_extra = list(allow_extra or [])
        self.env = dict(env or {})
        self.id = "(sin crear)"
        self._created = False
        self._blocked: list[EgressDecision] = []

    async def _call(self, op: str, wait_s: float = 60, **params: Any) -> dict[str, Any]:
        if not self.socket_path.exists():
            raise SandboxError(f"broker no disponible ({self.socket_path})")
        reader, writer = await asyncio.open_unix_connection(
            str(self.socket_path), limit=4 * 1024 * 1024
        )
        try:
            req = {"op": op, "session_id": self.session_id, **params}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), wait_s)
        finally:
            writer.close()
        resp = json.loads(line or b"{}")
        if "error" in resp:
            raise SandboxError(f"broker: {resp['error']}")
        return resp

    async def _ensure(self) -> None:
        if not self._created:
            resp = await self._call(
                "create", wait_s=120, allow_extra=self.allow_extra, env=self.env
            )
            self.id = resp["id"]
            self._created = True

    async def exec(self, command: str, timeout_s: int | None = None) -> ExecResult:
        await self._ensure()
        resp = await self._call(
            "exec", wait_s=(timeout_s or 600) + 30, command=command, timeout_s=timeout_s
        )
        self._blocked = [EgressDecision(**b) for b in resp.get("egress_blocked", [])]
        return ExecResult(**resp["result"])

    def egress_blocked_since_last(self) -> list[EgressDecision]:
        out, self._blocked = self._blocked, []
        return out

    async def reset(self) -> None:
        await self._ensure()
        await self._call("reset", wait_s=120)

    async def destroy(self) -> None:
        if self._created:
            await self._call("destroy", wait_s=60)
            self._created = False
