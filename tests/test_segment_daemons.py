"""Un daemon por segmento (ADR-0026): Matrix (!osint) y el chat (relé) llegan a osint sin que el
orquestador de main toque su contenido (P2, RF-SEC-02)."""

from __future__ import annotations

import asyncio
import contextlib

import httpx
from test_matrix import OWNER, FakeHomeserver, pump
from test_server import call, final

from argos.audit.redact import Redactor
from argos.audit.store import AuditStore
from argos.channels.matrix.bridge import BridgeConfig, MatrixBridge
from argos.channels.matrix.client import MatrixClient
from argos.config import load_config
from argos.model.fake import FakeProvider
from argos.scheduler import SchedulerCfg, load_scheduler_cfg
from argos.server.app import Core, serve
from argos.server.client import CoreClient

SEGMENT_PROFILES = {"osint": "osint", "pentest": "pentest"}


def _cfg(root, segment):
    return load_config(
        root, {"data_dir": str(root / "var"), "model.provider": "fake", "segment": segment}
    )


@contextlib.asynccontextmanager
async def running_cores(root, fake_sandbox, osint_scripts=(), main_scripts=()):
    """Daemon de main y de osint en sus sockets reales (var/segments/<seg>/run/argos.sock)."""
    tasks, cores = [], {}
    for seg, scripts in (("main", list(main_scripts)), ("osint", list(osint_scripts))):
        cfg = _cfg(root, seg)
        store = AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))
        core = Core(
            cfg,
            store,
            lambda q=scripts: FakeProvider(q.pop(0) if q else [final()]),
            SchedulerCfg(),
        )
        core.manager.sandbox_factory = lambda: fake_sandbox
        tasks.append(
            asyncio.create_task(serve(core, cfg.api_socket, None, None, run_scheduler=False))
        )
        for _ in range(100):
            if cfg.api_socket.exists():
                break
            await asyncio.sleep(0.05)
        cores[seg] = core
    try:
        yield cores
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


def test_scheduler_keeps_only_its_segment(root):
    assert load_scheduler_cfg(_cfg(root, "main")).schedules  # trayecto, evaluación…
    osint = load_scheduler_cfg(_cfg(root, "osint"))
    assert osint.schedules == [] and osint.hooks == []


async def test_relay_reaches_other_segment_only_through_its_socket(root, fake_sandbox):
    async with running_cores(root, fake_sandbox, osint_scripts=[[final("hecho en osint")]]) as c:
        main_sock = c["main"].cfg.api_socket
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(main_sock)), base_url="http://argos"
        ) as raw:
            assert (await raw.get("/seg/osint/health")).json()["segment"] == "osint"
            assert (await raw.get("/seg/main/health")).status_code == 404  # no hay relé a sí mismo
            assert (await raw.get("/seg/nada/health")).status_code == 404
            assert (await raw.get("/seg/pentest/health")).status_code == 503  # parado
        # El chat remoto usa el mismo cliente con base /seg/osint: sesión y SSE completos.
        async with CoreClient(main_sock) as client:
            client._client.base_url = "http://argos/seg/osint"
            sid = await client.submit(task="t", profile="osint")
            events = [ev async for ev in client.events(sid)]
        assert events[-1]["type"] in ("session_end", "stream_end")
        assert any(e.get("result") == "hecho en osint" for e in events)
        # La sesión vive en osint; el orquestador (main) no la vio.
        assert [s["profile"] for s in c["osint"].store.sessions()] == ["osint"]
        assert c["main"].store.sessions() == []


async def test_matrix_routes_osint_threads_and_approvals(root, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    person = call(
        "osint.person",
        workflow="email_reputation",
        target="yo@ejemplo.com",
        purpose="comprobar si mi propio email aparece en brechas",
    )
    scripts = [[person, final("revisado")], [final("sigo en osint")]]
    async with running_cores(root, fake_sandbox, osint_scripts=scripts) as c:
        main_cfg = c["main"].cfg
        matrix = MatrixClient("http://hs", "token", transport=httpx.ASGITransport(app=hs.app()))
        async with CoreClient(main_cfg.api_socket) as main_client:
            bridge = MatrixBridge(
                matrix,
                main_client,
                BridgeConfig(
                    allowed_users=[OWNER],
                    progress_interval_s=0,
                    open_dm=False,
                    profile="orchestrator",
                    segment_profiles=SEGMENT_PROFILES,
                ),
                tmp_path / "matrix.db",
                segment_client=lambda seg: CoreClient.connect(
                    main_cfg.api_socket, main_cfg.root, segment=seg
                ),
            )
            await bridge.run(once=True)
            root_ev = hs.say("!osint mira si mi email está en alguna brecha")
            for _ in range(80):
                await bridge.run(once=True)
                await asyncio.sleep(0.05)
                if any("🔐" in t for t in hs.texts()):
                    break
            assert any("[osint]" in t and "osint.person" in t for t in hs.texts())
            hs.say("sí", thread=root_ev)  # la decisión va al daemon de osint
            await pump(bridge, until=lambda: "revisado" in hs.texts())
            hs.say("¿y algo más?", thread=root_ev)  # el hilo sigue en osint
            await pump(bridge, until=lambda: "sigo en osint" in hs.texts())
            hs.say("!pentest escanea mi web")
            hs.say("!estado")
            await pump(bridge, rounds=4)
            await bridge.aclose()
        await matrix.aclose()
        osint_sessions = c["osint"].store.sessions()
        approvals = [
            e for s in osint_sessions for e in c["osint"].store.events(s["id"], ["approval"])
        ]
    assert c["main"].store.sessions() == []  # el orquestador no vio nada
    assert {s["profile"] for s in osint_sessions} == {"osint"} and len(osint_sessions) == 2
    assert [(a.decision, a.channel) for a in approvals] == [("approved", "matrix")]
    texts = hs.texts()
    assert any("núcleo de pentest no está en marcha" in t for t in texts)
    status = next(t for t in texts if t.startswith("main:"))
    assert "osint: sesiones" in status and "pentest: no está en marcha" in status


async def test_osint_command_inside_main_thread_is_refused(root, fake_sandbox, tmp_path):
    hs = FakeHomeserver()
    async with running_cores(root, fake_sandbox, main_scripts=[[final("hola desde main")]]) as c:
        main_cfg = c["main"].cfg
        matrix = MatrixClient("http://hs", "token", transport=httpx.ASGITransport(app=hs.app()))
        async with CoreClient(main_cfg.api_socket) as main_client:
            bridge = MatrixBridge(
                matrix,
                main_client,
                BridgeConfig(
                    allowed_users=[OWNER],
                    progress_interval_s=0,
                    open_dm=False,
                    profile="orchestrator",
                    segment_profiles=SEGMENT_PROFILES,
                ),
                tmp_path / "matrix.db",
                segment_client=lambda seg: CoreClient.connect(
                    main_cfg.api_socket, main_cfg.root, segment=seg
                ),
            )
            await bridge.run(once=True)
            root_ev = hs.say("hola")
            await pump(bridge, until=lambda: "hola desde main" in hs.texts())
            hs.say("!osint investiga algo", thread=root_ev)
            await pump(bridge, rounds=3)
            await bridge.aclose()
        await matrix.aclose()
    assert any("fuera de un hilo" in t for t in hs.texts())
    assert c["osint"].store.sessions() == []


def _bridge(main_cfg, main_client, hs, tmp_path):
    matrix = MatrixClient("http://hs", "token", transport=httpx.ASGITransport(app=hs.app()))
    bridge = MatrixBridge(
        matrix,
        main_client,
        BridgeConfig(
            allowed_users=[OWNER],
            progress_interval_s=0,
            open_dm=False,
            profile="orchestrator",
            segment_profiles=SEGMENT_PROFILES,
        ),
        tmp_path / "matrix.db",
        segment_client=lambda seg: CoreClient.connect(
            main_cfg.api_socket, main_cfg.root, segment=seg
        ),
    )
    return matrix, bridge


async def test_orchestrator_hands_off_to_osint_without_seeing_result(root, fake_sandbox, tmp_path):
    """El usuario pide al orquestador (sin !) una investigación; este la pasa con agent.handoff al
    daemon de osint, que la ejecuta. El orquestador no ve el resultado (P2, ADR-0026)."""
    hs = FakeHomeserver()
    # El modelo resume la tarea sin el dominio a propósito: el canal debe reenviar el texto literal.
    main_scripts = [[call("agent.handoff", profile="osint", task="una investigación"),
                     final("Se lo paso a osint.")]]  # fmt: skip
    osint_scripts = [[call("osint.recon", workflow="domain_recon", target="ejemplo.com"),
                      final("ejemplo.com: 2 hallazgos")]]  # fmt: skip
    async with running_cores(root, fake_sandbox, osint_scripts=osint_scripts,
                             main_scripts=main_scripts) as c:  # fmt: skip
        main_cfg = c["main"].cfg
        async with CoreClient(main_cfg.api_socket) as main_client:
            matrix, bridge = _bridge(main_cfg, main_client, hs, tmp_path)
            await bridge.run(once=True)
            hs.say("investiga el dominio ejemplo.com")
            await pump(bridge, until=lambda: "ejemplo.com: 2 hallazgos" in hs.texts())
            await bridge.aclose()
        await matrix.aclose()
        main_sessions = c["main"].store.sessions()
        osint_sessions = c["osint"].store.sessions()
    # El orquestador corrió y pasó la tarea; el trabajo real está en osint.
    assert [s["profile"] for s in main_sessions] == ["orchestrator"]
    assert [s["profile"] for s in osint_sessions] == ["osint"]
    # Lo que llega a osint es el mensaje literal del usuario, no el resumen del modelo: de ahí
    # saldría el alcance autorizado de un pentest (RF-LEG-01).
    assert "el dominio ejemplo.com" in osint_sessions[0]["task"]
    recon = [e for s in osint_sessions for e in c["osint"].store.events(s["id"], ["tool_call"])]
    assert any(e.tool == "osint.recon" for e in recon)
    # El resultado de osint no entró en la sesión del orquestador.
    assert not any(
        e.tool and e.tool.startswith("osint.")
        for s in main_sessions
        for e in c["main"].store.events(s["id"], ["tool_call"])
    )
    assert "ejemplo.com: 2 hallazgos" in hs.texts()


def test_orchestrator_knows_isolated_profiles_but_cannot_delegate(root):
    from argos.core.session import isolated_profiles
    from argos.core.subagent import DelegateTool

    cfg = _cfg(root, "main")
    iso = isolated_profiles(cfg)
    assert set(iso) == {"osint", "pentest"}
    tool = DelegateTool(None, 1000, "orchestrator", {"infra": "infra"}, iso)
    assert "AISLADOS" in tool.description and "agent.handoff" in tool.description
    assert "osint" not in tool._targets  # informativo: sigue sin poder delegar ahí


async def test_handoff_tool_emits_event_without_running(root):
    from argos.audit.events import HandoffRequested
    from argos.core.session import isolated_profiles
    from argos.core.subagent import HandoffTool
    from argos.tools.base import ToolContext

    cfg = _cfg(root, "main")
    emitted = []
    tool = HandoffTool(isolated_profiles(cfg), lambda n: cfg.segment_of(n) or "")
    ctx = ToolContext(
        session_id="s1",
        turn_id="t",
        trace_id="s1",
        profile=cfg.profile("orchestrator"),
        workspace=root,
        store=None,
        emit=emitted.append,
    )
    res = await tool.run({"profile": "pentest", "task": "audita cm.playingwith.info"}, ctx)
    assert res.ok and res.data["handoff_profile"] == "pentest"
    assert len(emitted) == 1 and isinstance(emitted[0], HandoffRequested)
    assert emitted[0].target_segment == "pentest" and "cm.playingwith.info" in emitted[0].task
    # No se puede pasar a un perfil del propio segmento ni a uno inexistente.
    from argos.tools.base import ToolError

    for bad in ("infra", "noexiste"):
        try:
            await tool.run({"profile": bad, "task": "x"}, ctx)
            raise AssertionError(f"{bad} debería rechazarse")
        except ToolError:
            pass


def test_request_authorizes_pentest_scope(root):
    """RF-LEG-01: el alcance sale del objetivo que el usuario nombra en su petición, con una
    referencia auto-generada; se acumula por hilo. Sin objetivo, falla cerrado."""
    from argos.core.session import SessionOptions, session_scope
    from argos.pentest import ScopeError
    from argos.state import StateStore

    cfg = _cfg(root, "pentest")
    pentest = cfg.profile("pentest")
    state = StateStore(root / "st.db")
    state.db.execute(
        "INSERT INTO threads(id,title,channel,profile,created_at,updated_at)"
        " VALUES ('th','t','matrix','pentest','now','now')"
    )
    state.db.commit()

    sc = session_scope(
        SessionOptions(task="audita https://cm.playingwith.info", profile="pentest",
                       channel="matrix", thread_id="th"),
        pentest, state,
    )  # fmt: skip
    assert sc.authorized and sc.allows("cm.playingwith.info")
    assert "usuario" in (sc.authorization_ref or "")
    sc.check(["cm.playingwith.info"])
    try:
        sc.check(["otro.ajeno.com"])
        raise AssertionError("un host no nombrado debe quedar fuera de alcance")
    except ScopeError:
        pass
    # Continuidad: un segundo mensaje añade otro objetivo sin perder el primero.
    sc2 = session_scope(
        SessionOptions(task="ahora isms.playingwith.info", profile="pentest",
                       channel="matrix", thread_id="th"),
        pentest, state,
    )  # fmt: skip
    assert sc2.allows("cm.playingwith.info") and sc2.allows("isms.playingwith.info")
    # Sin objetivo nombrado y sin historial: sin autorización (falla cerrado).
    empty = session_scope(
        SessionOptions(task="hola", profile="pentest", channel="chat"), pentest, None
    )
    assert not empty.authorized
