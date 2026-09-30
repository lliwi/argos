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
        self.created: list[dict] = []
        self.direct: dict = {}
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

        async def create_room(request: Request):
            self.created.append(await request.json())
            return JSONResponse({"room_id": f"!dm{len(self.created)}:test"})

        async def direct(request: Request):
            if request.method == "PUT":
                self.direct = await request.json()
                return JSONResponse({})
            if not self.direct:
                return JSONResponse({"errcode": "M_NOT_FOUND"}, 404)
            return JSONResponse(self.direct)

        base = "/_matrix/client/v3"
        return Starlette(routes=[
            Route(base + "/createRoom", create_room, methods=["POST"]),
            Route(base + "/user/{user}/account_data/m.direct", direct, methods=["GET", "PUT"]),
            Route(base + "/account/whoami", whoami),
            Route(base + "/sync", sync),
            Route(base + "/rooms/{room}/join", join, methods=["POST"]),
            Route(base + "/rooms/{room}/send/{etype}/{txn}", send, methods=["PUT"]),
        ])

    def texts(self) -> list[str]:
        return [s["content"].get("m.new_content", s["content"]).get("body", "")
                for s in self.sent if s["type"] == "m.room.message"]


async def make_bridge(hs: FakeHomeserver, core_client, tmp_path,
                      open_dm: bool = False) -> MatrixBridge:
    matrix = MatrixClient("http://hs", "token", transport=httpx.ASGITransport(app=hs.app()))
    bridge = MatrixBridge(matrix, core_client, BridgeConfig(
        allowed_users=[OWNER], progress_interval_s=0, open_dm=open_dm), tmp_path / "matrix.db")
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


async def test_opens_unencrypted_dm_once(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path, open_dm=True)
        await bridge.run(once=True)                       # reinicio: no crea otro
    assert len(hs.created) == 1
    room = hs.created[0]
    assert room["is_direct"] is True and room["invite"] == [OWNER]
    assert room["preset"] == "trusted_private_chat"
    assert not any(e.get("type") == "m.room.encryption" for e in room["initial_state"])
    assert hs.direct == {OWNER: ["!dm1:test"]}
    assert "Hola, soy Argos" in hs.texts()[0]


async def test_warns_once_in_encrypted_rooms(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path, open_dm=True)
        for sender in (OWNER, OWNER, STRANGER):
            hs.pending.append((ROOM, {"type": "m.room.encrypted", "event_id": hs.eid(),
                                      "sender": sender, "content": {"algorithm": "m.megolm"}}))
        await pump(bridge, rounds=2)
    warnings = [t for t in hs.texts() if t.startswith("🔒")]
    assert len(warnings) == 1 and "chat «Argos»" in warnings[0]
    assert store.sessions() == []


async def test_login_uses_localpart_and_reports_errors():
    import pytest

    from argos.channels.matrix.client import MatrixError, login

    seen = {}

    async def handler(request: Request):
        seen.update(await request.json())
        if seen["password"] != "ok":
            return JSONResponse({"errcode": "M_FORBIDDEN", "error": "Invalid password"}, 403)
        return JSONResponse({"access_token": "t", "user_id": BOT, "device_id": "D"})

    app = Starlette(routes=[Route("/_matrix/client/v3/login", handler, methods=["POST"])])
    t = httpx.ASGITransport(app=app)
    data = await login("http://hs", "@argos:test", "ok", transport=t)
    assert data["device_id"] == "D" and seen["identifier"] == {"type": "m.id.user",
                                                               "user": "argos"}
    with pytest.raises(MatrixError, match="M_FORBIDDEN"):
        await login("http://hs", "argos", "mal", transport=t)


async def test_scheduled_result_reaches_the_dm(cfg, store, fake_sandbox, tmp_path):
    from argos.scheduler import ScheduleCfg, SchedulerCfg

    hs = FakeHomeserver()
    sched = SchedulerCfg(schedules=[
        ScheduleCfg(name="trayecto", title="Trayecto trabajo", cron="0 18 * * 0-4", task="t"),
        ScheduleCfg(name="silenciosa", cron="0 9 * * *", task="t", notify=False)])
    scripts = [[final("🚲 Bici: mañana seco")], [final("no debería llegar")]]
    async with running_core(cfg, store, scripts, fake_sandbox, sched=sched) as (core, client):
        assert sched.schedules[0].profile == "orchestrator"      # punto de entrada por defecto
        bridge = await make_bridge(hs, client, tmp_path, open_dm=True)
        await asyncio.sleep(0.2)                                 # observador global suscrito
        core.scheduler.fire(sched.schedules[0])
        core.scheduler.fire(sched.schedules[1])
        for _ in range(60):
            await asyncio.sleep(0.05)
            if any(t.startswith("⏰") for t in hs.texts()):
                break
        await asyncio.sleep(0.3)
        bridge._watcher.cancel()
    reports = [(s["room"], s["content"]["body"]) for s in hs.sent
               if s["content"].get("body", "").startswith("⏰")]
    assert reports == [("!dm1:test", "⏰ Trayecto trabajo\n🚲 Bici: mañana seco")]


async def test_eval_report_reaches_the_dm(cfg, store, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    async with running_core(cfg, store, [], fake_sandbox) as (core, client):
        bridge = await make_bridge(hs, client, tmp_path, open_dm=True)
        await asyncio.sleep(0.2)
        core.bus.publish({"type": "eval_report", "title": "Evaluación de Argos",
                          "notify": True, "text": "✅ 9 tareas · éxito 100%"})
        core.bus.publish({"type": "eval_report", "title": "silenciosa", "notify": False,
                          "text": "no"})
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(t.startswith("📊") for t in hs.texts()):
                break
        await asyncio.sleep(0.2)
        bridge._watcher.cancel()
    assert [t for t in hs.texts() if t.startswith("📊")] == [
        "📊 Evaluación de Argos\n✅ 9 tareas · éxito 100%"]
