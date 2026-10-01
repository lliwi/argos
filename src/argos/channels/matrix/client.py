"""Cliente mínimo de la API cliente-servidor de Matrix (v3) sobre httpx.

Solo lo que necesita el puente: identidad, sync largo, unirse a salas, enviar/editar mensajes
(con hilos `m.thread`) y reacciones. Sin cifrado extremo a extremo: el bot funciona en salas no
cifradas (ADR-0012).
"""

from __future__ import annotations

import itertools
import time
from typing import Any
from urllib.parse import quote

import httpx


class MatrixError(RuntimeError):
    pass


class MatrixClient:
    def __init__(
        self,
        homeserver: str,
        access_token: str,
        *,
        timeout: float = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=homeserver.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._txn = itertools.count(int(time.time() * 1000))
        self.user_id: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._http.request(method, "/_matrix/client/v3" + path, **kw)
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"error": resp.text[:200]}
            raise MatrixError(
                f"{method} {path}: {resp.status_code} "
                f"{err.get('errcode', '')} {err.get('error', '')}"
            )
        return resp.json() if resp.content else {}

    async def whoami(self) -> str:
        self.user_id = (await self._req("GET", "/account/whoami"))["user_id"]
        return self.user_id

    async def sync(self, since: str | None, timeout_ms: int = 30_000) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_ms if since else 0}
        if since:
            params["since"] = since
        # Solo lo que usa el puente: mensajes, reacciones e invitaciones; sin presencia.
        # m.room.encrypted: solo para avisar de que en salas cifradas no se lee nada.
        params["filter"] = (
            '{"presence":{"not_types":["*"]},"account_data":{"not_types":["*"]},'
            '"room":{"ephemeral":{"not_types":["*"]},'
            '"timeline":{"types":["m.room.message","m.reaction",'
            '"m.room.encrypted"]}}}'
        )
        return await self._req("GET", "/sync", params=params, timeout=timeout_ms / 1000 + 30)

    async def create_dm(self, user_id: str, name: str = "Argos", topic: str = "") -> str:
        """Conversación directa SIN cifrar con `user_id` (sin estado m.room.encryption)."""
        data = await self._req(
            "POST",
            "/createRoom",
            json={
                "is_direct": True,
                "invite": [user_id],
                "preset": "trusted_private_chat",
                "name": name,
                "topic": topic,
                "initial_state": [],
            },
        )
        room_id = data["room_id"]
        # Que los clientes la muestren como chat directo (m.direct es del bot, se fusiona).
        me = quote(self.user_id or await self.whoami())
        try:
            direct = await self._req("GET", f"/user/{me}/account_data/m.direct")
        except MatrixError:
            direct = {}
        direct.setdefault(user_id, [])
        if room_id not in direct[user_id]:
            direct[user_id].append(room_id)
        await self._req("PUT", f"/user/{me}/account_data/m.direct", json=direct)
        return room_id

    async def is_encrypted(self, room_id: str) -> bool:
        try:
            await self._req("GET", f"/rooms/{quote(room_id)}/state/m.room.encryption")
        except MatrixError:
            return False
        return True

    async def joined_members(self, room_id: str) -> list[str]:
        data = await self._req("GET", f"/rooms/{quote(room_id)}/joined_members")
        return list((data.get("joined") or {}).keys())

    async def join(self, room_id: str) -> None:
        await self._req("POST", f"/rooms/{quote(room_id)}/join", json={})

    async def _send(self, room_id: str, event_type: str, content: dict[str, Any]) -> str:
        txn = next(self._txn)
        data = await self._req(
            "PUT", f"/rooms/{quote(room_id)}/send/{event_type}/{txn}", json=content
        )
        return data["event_id"]

    async def send_text(
        self, room_id: str, text: str, thread_root: str | None = None, notice: bool = False
    ) -> str:
        content: dict[str, Any] = {"msgtype": "m.notice" if notice else "m.text", "body": text}
        if thread_root:
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": thread_root,
                # Clientes sin soporte de hilos lo ven como respuesta al mensaje raíz.
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": thread_root},
            }
        return await self._send(room_id, "m.room.message", content)

    async def edit_text(self, room_id: str, event_id: str, text: str, notice: bool = True) -> str:
        msgtype = "m.notice" if notice else "m.text"
        return await self._send(
            room_id,
            "m.room.message",
            {
                "msgtype": msgtype,
                "body": f"* {text}",
                "m.new_content": {"msgtype": msgtype, "body": text},
                "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
            },
        )

    async def react(self, room_id: str, event_id: str, key: str) -> str:
        return await self._send(
            room_id,
            "m.reaction",
            {"m.relates_to": {"rel_type": "m.annotation", "event_id": event_id, "key": key}},
        )


async def login(
    homeserver: str,
    user: str,
    password: str,
    device_name: str = "argos",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Login por contraseña. Devuelve {access_token, user_id, device_id}. No guarda nada."""
    localpart = user[1:].split(":", 1)[0] if user.startswith("@") else user
    async with httpx.AsyncClient(
        base_url=homeserver.rstrip("/"), timeout=30, transport=transport
    ) as http:
        resp = await http.post(
            "/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": localpart},
                "password": password,
                "initial_device_display_name": device_name,
            },
        )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except ValueError:
            err = {}
        raise MatrixError(
            f"login: {resp.status_code} {err.get('errcode', '')} {err.get('error', '')}".strip()
        )
    return resp.json()
