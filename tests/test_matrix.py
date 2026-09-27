"""Canal Matrix (RF-07, RF-20) contra un homeserver simulado y el núcleo real."""

from __future__ import annotations

import asyncio
import itertools

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from test_server import call, final, running_core

from argos.channels.matrix.bridge import BridgeConfig, MatrixBridge
from argos.channels.matrix.client import MatrixClient

BOT, OWNER, STRANGER, ROOM = "@argos:test", "@lliwi:test", "@intruso:test", "!sala:test"


class FakeHomeserver:
    def __init__(self) -> None:
        self.pending: list[tuple[str, dict]] = []     # (room, evento) para el próximo sync
        self.invites: dict[str, str] = {}             # room -> invitador
        self.sent: list[dict] = []
        self.joined: list[str] = []
        self._ids = itertools.count(1)
        self._batch = itertools.count(1)

    def eid(self) -> str:
        return f"$ev{next(self._ids)}"

    def say(self, body: str, sender: str = OWNER, thread: str | None = None) -> str:
        content: dict = {"msgtype": "m.text", "body": body}
        if thread:
            content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread}
        ev = {"type": "m.room.message", "event_id": self.eid(), "sender": sender,
              "content": content}
        self.pending.append((ROOM, ev))
        return ev["event_id"]

    def react(self, event_id: str, key: str, sender: str = OWNER) -> None:
        self.pending.append((ROOM, {"type": "m.reaction", "event_id": self.eid(), "sender": sender,
                                    "content": {"m.relates_to": {"rel_type": "m.annotation",
                                                                 "event_id": event_id,
                                                                 "key": key}}}))

    def app(self) -> Starlette:
        async def whoami(_):
            return JSONResponse({"user_id": BOT})

        async def sync(_):
            rooms: dict = {"join": {}, "invite": {}}
            for room, ev in self.pending:
                rooms["join"].setdefault(room, {"timeline": {"events": []}})
                rooms["join"][room]["timeline"]["events"].append(ev)
            for room, inviter in self.invites.items():
                rooms["invite"][room] = {"invite_state": {"events": [
                    {"type": "m.room.member", "state_key": BOT, "sender": inviter}]}}
            self.pending, self.invites = [], {}
            return JSONResponse({"next_batch": f"b{next(self._batch)}", "rooms": rooms})

        async def join(request: Request):
            self.joined.append(request.path_params["room"])
            return JSONResponse({"room_id": request.path_params["room"]})

        async def send(request: Request):
            eid = self.eid()
            self.sent.append({"room": request.path_params["room"], "event_id": eid,
                              "type": request.path_params["etype"],
                              "content": await request.json()})
            return JSONResponse({"event_id": eid})

        base = "/_matrix/client/v3"
        return Starlette(routes=[
            Route(base + "/account/whoami", whoami),
            Route(base + "/sync", sync),
            Route(base + "/rooms/{room}/join", join, methods=["POST"]),
            Route(base + "/rooms/{room}/send/{etype}/{txn}", send, methods=["PUT"]),
        ])

    def texts(self) -> list[str]:
        return [s["content"].get("m.new_content", s["content"]).get("body", "")
                for s in self.sent if s["type"] == "m.room.message"]


async def make_bridge(hs: FakeHomeserver, core_client, tmp_path) -> MatrixBridge:
    matrix = MatrixClient("http://hs", "token", transport=httpx.ASGITransport(app=hs.app()))
    bridge = MatrixBridge(matrix, core_client, BridgeConfig(
        allowed_users=[OWNER], progress_interval_s=0), tmp_path / "matrix.db")
    await bridge.run(once=True)          # primer arranque: fija el punto de partida
    return bridge


async def pump(bridge: MatrixBridge, until=lambda: False, rounds: int = 60) -> None:
    for _ in range(rounds):
        await bridge.run(once=True)
        await asyncio.sleep(0.05)
        if until() and not bridge.runs:
            break
    await asyncio.gather(*bridge.tasks, return_exceptions=True)


async def test_backlog_ignored_invites_and_strangers(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    hs.say("mensaje viejo")                              # llega en el primer sync: se ignora
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        hs.invites = {"!buena:test": OWNER, "!mala:test": STRANGER}
        bridge = await make_bridge(hs, client, tmp_path)
        await bridge.run(once=True)
        hs.say("haz algo", sender=STRANGER)
        await pump(bridge, rounds=3)
    assert hs.joined == ["!buena:test"]
    assert store.sessions() == []                         # ni el viejo ni el intruso


async def test_task_in_thread_with_continuity(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    scripts = [[final("Anotado: tu perro es Tofu")], [final("Se llama Tofu")]]
    async with running_core(cfg, store, scripts, fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path)
        root = hs.say("Mi perro se llama Tofu")
        await pump(bridge, until=lambda: "Anotado: tu perro es Tofu" in hs.texts())
        hs.say("¿Cómo se llama mi perro?", thread=root)
        await pump(bridge, until=lambda: "Se llama Tofu" in hs.texts())
        threads = core.state.threads()
    assert "Anotado: tu perro es Tofu" in hs.texts() and "Se llama Tofu" in hs.texts()
    replies = [s for s in hs.sent if s["content"].get("body") == "Se llama Tofu"]
    assert replies[0]["content"]["m.relates_to"]["event_id"] == root     # en el hilo
    assert len(threads) == 1 and len(core.state.exchanges(threads[0].id)) == 2
    assert any("✅ completed" in t for t in hs.texts())                  # progreso editado


async def test_approval_by_reply_and_by_reaction(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    scripts = [[call("shell.exec", command="rm -rf /home/agent/a"), final("limpio a")],
               [call("shell.exec", command="rm -rf /home/agent/b"), final("no hecho")]]
    async with running_core(cfg, store, scripts, fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path)
        root = hs.say("limpia a")

        def approval_asked(n):
            return lambda: sum("🔐" in t for t in hs.texts()) >= n

        for _ in range(60):
            await bridge.run(once=True)
            await asyncio.sleep(0.05)
            if approval_asked(1)():
                break
        hs.say("sí", thread=root)
        await pump(bridge, until=lambda: "limpio a" in hs.texts())

        hs.say("limpia b")
        for _ in range(60):
            await bridge.run(once=True)
            await asyncio.sleep(0.05)
            if approval_asked(2)():
                break
        ask = [s for s in hs.sent if "🔐" in s["content"].get("body", "")][-1]
        hs.react(ask["event_id"], "❌")
        await pump(bridge, until=lambda: any("denegada" in t for t in hs.texts()))
    assert fake_sandbox.commands == ["rm -rf /home/agent/a"]
    approvals = [e for s in store.sessions() for e in store.events(s["id"], ["approval"])]
    got = sorted((a.decision, a.approver, a.channel) for a in approvals)
    assert got == [("approved", OWNER, "matrix"), ("denied", OWNER, "matrix")]


async def test_kill_switch_from_matrix(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path)
        hs.say("!kill en el móvil")
        await pump(bridge, rounds=2)
        assert core.kill.active() == "en el móvil"
        hs.say("!rearm")
        await pump(bridge, rounds=2)
        assert core.kill.active() is None
