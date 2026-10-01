"""CLI `argos`: ejecutar tareas, revisar auditoría, evaluar y gobernar."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.table import Table

from argos import retention
from argos.audit.events import Feedback
from argos.audit.redact import Redactor
from argos.audit.review import aggregate_metrics, diff_sessions, replay_lines, summarize
from argos.audit.store import AuditStore
from argos.channels.cli import cli_approver, console, progress_printer
from argos.config import Config, load_config
from argos.core.session import SessionOptions, SessionRefused, run_session
from argos.eval.runner import compare_runs, compare_to_baseline, run_suite
from argos.governance.killswitch import KillSwitch
from argos.model import factory
from argos.model.base import ModelProvider
from argos.skills import SkillError, SkillRegistry

app = typer.Typer(help="Argos — arnés de agentes auditable y evaluable.", no_args_is_help=True)
audit_app = typer.Typer(help="Revisión de auditoría (RF-OB-09).", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluación y regresión (§11).", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
skills_app = typer.Typer(help="Skills instaladas (§9).", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
app.add_typer(skills_app, name="skills")


def _ctx() -> tuple[Config, AuditStore]:
    cfg = load_config()
    return cfg, AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))


def make_provider(cfg: Config, name: str | None = None) -> ModelProvider:
    try:
        return factory.make_provider(cfg, name)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


# --- ejecución ---------------------------------------------------------------------------------


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="Tarea en lenguaje natural")],
    profile: Annotated[str, typer.Option("--profile", "-p")] = "personal",
    provider: Annotated[str | None, typer.Option(help="codex | fake")] = None,
    dry_run: Annotated[bool | None, typer.Option("--dry-run/--no-dry-run")] = None,
    allow_domain: Annotated[list[str] | None, typer.Option(help="Dominio extra de egress")] = None,
    budget: Annotated[int | None, typer.Option(help="Tope de tokens de la sesión")] = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q")] = False,
) -> None:
    """Ejecuta una tarea en una sesión nueva (canal CLI)."""
    cfg, store = _ctx()
    opts = SessionOptions(
        task=task,
        profile=profile,
        channel="cli",
        dry_run=dry_run,
        allow_domains=allow_domain or [],
        session_budget_tokens=budget,
    )
    try:
        result = asyncio.run(
            run_session(
                opts,
                cfg,
                make_provider(cfg, provider),
                store=store,
                approver=cli_approver(),
                on_progress=progress_printer(verbose=not quiet),
            )
        )
    except SessionRefused as exc:
        console.print(f"[red]Sesión rechazada:[/] {exc}")
        raise typer.Exit(2) from exc
    color = "green" if result.status == "completed" else "red"
    console.print(
        f"\n[{color}]{result.status}[/] · sesión {result.session_id} · "
        f"{result.steps} pasos · {result.tokens} tokens · workspace {result.workspace}"
    )
    if result.status != "completed":
        console.print(f"[{color}]{result.message}[/]")
        raise typer.Exit(1)


# --- núcleo persistente (RF-04) -----------------------------------------------------------------

core_app = typer.Typer(
    help="Núcleo persistente: API, sesiones en segundo plano, scheduler.", no_args_is_help=True
)
app.add_typer(core_app, name="core")


@app.command()
def serve(
    hooks_port: Annotated[int | None, typer.Option(help="Puerto TCP para webhooks")] = None,
    hooks_host: Annotated[str, typer.Option(help="Interfaz de webhooks")] = "127.0.0.1",
) -> None:
    """Arranca el núcleo persistente: API en socket Unix + scheduler (+ webhooks)."""
    from argos.scheduler import load_scheduler_cfg
    from argos.server.app import Core
    from argos.server.app import serve as run_server

    cfg, store = _ctx()
    core = Core(cfg, store, lambda: make_provider(cfg), load_scheduler_cfg(cfg))
    console.print(
        f"[green]Argos[/] segmento [bold]{cfg.segment}[/] · API {cfg.api_socket}"
        + (f" · webhooks {hooks_host}:{hooks_port}" if hooks_port else "")
        + f" · {len(core.scheduler.sched.schedules)} tareas programadas"
    )
    try:
        asyncio.run(run_server(core, cfg.api_socket, hooks_host, hooks_port))
    except KeyboardInterrupt:
        console.print("núcleo detenido")


def _client():
    from argos.server.client import CoreClient, CoreUnavailable

    cfg = load_config()
    try:
        return CoreClient(cfg.api_socket)
    except CoreUnavailable as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(2) from exc


def _compact_line(data: dict, root: str) -> str | None:
    """Chat: la respuesta destacada; lo que hace el agente, atenuado y en una línea."""
    from rich.markup import escape

    kind = data.get("type")
    sub_ = data.get("session_id") != root
    pad = "    " if sub_ else "  "
    if kind == "session" and sub_:
        return f"[dim]{pad}↳ subagente: {escape(data['task'][:80])}[/]"
    if kind == "turn" and data.get("purpose") == "decide":
        d = data.get("decision") or {}
        if d.get("type") == "tool_call":
            args = json.dumps(d.get("args") or {}, ensure_ascii=False)
            args = args if len(args) <= 70 else args[:70] + "…"
            return f"[dim]{pad}· {escape(str(d.get('tool')))} {escape(args)}[/]"
    if kind == "tool_call" and data.get("status") in ("error", "denied"):
        return f"[dim red]{pad}  ✗ {escape((data.get('result_preview') or '')[:100])}[/]"
    if kind == "memory_event" and data.get("op") == "save":
        return f"[dim]{pad}· recordado: {escape(data.get('detail', '')[:90])}[/]"
    if kind == "error_event" and data.get("kind") in (
        "budget_exceeded",
        "loop_detected",
        "model_error",
        "killed",
    ):
        return f"[red]{pad}{data['kind']}: {escape(data['message'][:120])}[/]"
    if kind == "session_end" and not sub_:
        color = "" if data["status"] == "completed" else "red"
        answer = escape(data.get("result") or "")
        footer = f"[dim]({data['status']} · {data.get('steps', 0)} pasos)[/]"
        return f"\n[{color or 'bold'}]{answer}[/]\n{footer}"
    return None


async def _attach(sid: str, interactive: bool = True, compact: bool = False, on_state=None) -> str:
    """Muestra el progreso en vivo y resuelve aprobaciones desde la terminal (canal CLI).
    `on_state(state, message)` (async) recibe blocked/working al pedir y resolver aprobaciones."""
    import sys

    from argos.audit.events import parse_event
    from argos.audit.review import render_event

    _, store = _ctx()
    status = "desconocido"
    async with _client() as client:
        async for data in client.events(sid):
            if data.get("type") == "stream_end":
                status = data.get("status", status)
                console.print(f"[bold]FIN[/] estado={status}: {data.get('message', '')}")
                continue
            if data.get("type") == "approval_request":
                console.print(
                    f"\n[bold yellow]APROBACIÓN {data['id']}[/] {data['action']} "
                    f"(riesgo {data['risk_class']}, {data['timeout_s']}s)"
                )
                console.print(data["details"], markup=False, highlight=False)
                if on_state:
                    await on_state("blocked", f"aprobación: {data['action']}"[:80])
                if not (interactive and sys.stdin.isatty()):
                    console.print(f"  responde con: argos core approve {data['id']} [--deny]")
                    continue
                try:
                    answer = await asyncio.wait_for(
                        asyncio.to_thread(input, "¿Aprobar? [s/N] "), data["timeout_s"]
                    )
                except TimeoutError:
                    continue
                decision = (
                    "approved" if answer.strip().lower() in ("s", "si", "sí", "y") else "denied"
                )
                await client.decide(data["id"], decision, "cli-user", "cli")
                if on_state:
                    await on_state("working", "continuando tras la aprobación")
                continue
            try:
                event = parse_event(data)
            except (KeyError, ValueError):
                continue
            if compact:
                line = _compact_line(data, sid)
                if line:
                    console.print(line, highlight=False)
                if data.get("type") == "session_end" and data.get("session_id") == sid:
                    status = data.get("status", status)
                continue
            for line in render_event(event, store):
                console.print(line, highlight=False)
            if data.get("type") == "session_end" and data.get("session_id") == sid:
                status = data.get("status", status)
    return status


@app.command("console")
def console_cmd() -> None:
    """Consola de operador: actividad de todas las sesiones y aprobaciones (canal TUI).
    Dentro de un pane de Herdr informa además de su estado y notifica aprobaciones."""
    from argos.channels.console import HerdrReporter, run_console

    reporter = HerdrReporter()
    console.print(
        "[bold]Argos · consola[/]"
        + (f" · Herdr pane {reporter.pane}" if reporter.enabled else " · (sin Herdr)")
    )

    async def go() -> None:
        async with _client() as client:
            await run_console(client, reporter, console)

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


@app.command("chat")
def chat_cmd(
    profile: Annotated[str, typer.Option("--profile", "-p")] = "orchestrator",
    thread: Annotated[str | None, typer.Option(help="Retomar un hilo existente")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Detalle técnico")] = False,
) -> None:
    """Conversación con el núcleo persistente. Cada mensaje es una sesión auditada, encadenada
    al mismo hilo para que Argos recuerde de qué se habla. Flechas para editar e historial,
    Alt-Enter salto de línea, Ctrl-V pega una imagen; /adjuntar, /new, /threads, /memoria,
    /help. Ctrl-D para salir."""
    import shlex
    import sys

    from argos.attachments import AttachmentError
    from argos.channels.chat_input import ChatInput, Pending
    from argos.channels.console import HerdrReporter
    from argos.state import StateStore

    cfg = load_config()
    state = StateStore(cfg.data_path / "state.db")
    current: dict[str, str | None] = {"thread": thread}
    pending = Pending()
    # Dentro de Herdr, el chat se anuncia como agente (lista «agentes» de la barra lateral).
    herdr = HerdrReporter()
    if herdr.enabled:
        sys.stdout.write("\033]0;argos · chat\007")  # título del pane: lo localiza el plugin
        sys.stdout.flush()

    def report(st: str, msg: str) -> None:
        asyncio.run(herdr.state(st, msg))

    async def new_thread(title: str) -> str:
        async with _client() as client:
            return (await client.create_thread(title, profile, "chat"))["id"]

    async def one(task: str, attachments: list) -> None:
        if not current["thread"]:
            current["thread"] = await new_thread(task[:60])
        await herdr.state("working", task[:80])
        async with _client() as client:
            sid = await client.submit(
                task=task,
                profile=profile,
                channel="chat",
                thread_id=current["thread"],
                attachments=[a.to_api() for a in attachments],
            )
        if verbose:
            console.print(f"[dim]sesión {sid[:12]} · hilo {current['thread']}[/]")
        await _attach(sid, compact=not verbose, on_state=herdr.state)

    def command(line: str) -> None:
        cmd, _, rest = line.partition(" ")
        if cmd == "/adjuntar":
            try:
                paths = shlex.split(rest)
            except ValueError as exc:
                console.print(f"[red]{exc}[/]")
                return
            if not paths:
                console.print(
                    "uso: /adjuntar <ruta> [más rutas]  (también: arrastra el fichero "
                    "o Ctrl-V con una imagen copiada)"
                )
                return
            for p in paths:
                try:
                    console.print(f"📎 adjuntado {pending.add_paths([p])[0]}", highlight=False)
                except AttachmentError as exc:
                    console.print(f"[red]{exc}[/]")
        elif cmd == "/adjuntos":
            if not pending.items:
                console.print("[dim]sin adjuntos pendientes[/]")
            for i, a in enumerate(pending.items, 1):
                kind = "imagen" if a.is_image else "fichero"
                console.print(f"{i}. {a.name} ({kind}, {len(a.data) // 1024} KB)", highlight=False)
        elif cmd == "/quitar":
            gone = pending.remove(rest.strip())
            console.print(f"quitado: {', '.join(gone)}" if gone else "[dim]nada que quitar[/]")
        elif cmd == "/new":
            current["thread"] = None
            console.print("[dim]nueva conversación[/]")
        elif cmd == "/threads":
            for t in state.threads(10):
                mark = "›" if t.id == current["thread"] else " "
                console.print(
                    f"{mark} {t.id}  {t.updated_at[:16]}  {t.title}", markup=False, highlight=False
                )
            console.print("[dim]retoma uno con: argos chat --thread <id>[/]")
        elif cmd in ("/memoria", "/memory"):
            for m in state.memories(profile, limit=15):
                who = "tú" if m.provenance == "user" else "agente"
                pin = "📌" if m.pinned else " "
                console.print(
                    f"{pin} {m.id} [{m.kind} · {who}] {m.content}", markup=False, highlight=False
                )
        elif cmd in ("/herramientas", "/tools"):

            async def show_tools() -> None:
                async with _client() as client:
                    console.print(_render_tools(await client.tools(profile)))

            asyncio.run(show_tools())
        else:
            console.print(
                "/adjuntar <ruta>  adjuntar fichero/imagen · /adjuntos  ver · /quitar [n]  "
                "descartar · /new  nueva conversación · /threads  hilos · /memoria  lo que "
                "recuerda · /herramientas  tools del perfil\n"
                "Flechas: editar e historial · Alt-Enter: salto de línea · Ctrl-V: pegar imagen "
                "· arrastra ficheros para adjuntarlos · Ctrl-D: salir"
            )

    where = f"hilo {thread}" if thread else "conversación nueva"
    console.print(f"[bold]Argos[/] · perfil {profile} · {where} · /help para comandos")
    interactive = sys.stdin.isatty()
    prompt = ChatInput(cfg.data_path / "chat_history", pending) if interactive else None
    report("idle", "listo")
    try:
        while True:
            try:
                if prompt:
                    console.print()
                    task = prompt.read().strip()
                else:
                    task = input("\nargos> ").strip()
            except KeyboardInterrupt:
                continue  # Ctrl-C en el prompt: descarta la línea
            except EOFError:
                console.print()
                return
            if not task and not pending.items:
                continue
            if task.startswith("/"):
                command(task)
                continue
            if not task:
                task = "Revisa los adjuntos."
            attachments = pending.take()
            try:
                asyncio.run(one(task, attachments))
            except KeyboardInterrupt:
                console.print(
                    "[yellow]desconectado (la sesión sigue en el núcleo; "
                    "`argos core attach` para retomarla)[/]"
                )
            except RuntimeError as exc:
                console.print(f"[red]{exc}[/]")
                pending.items[:0] = attachments  # no se pierden si el envío falló
            report("idle", "listo")
    finally:
        asyncio.run(herdr.release())


@app.command("matrix")
def matrix_cmd() -> None:
    """Puente Matrix: tareas por mensaje, progreso en hilos y aprobaciones desde el móvil."""
    import logging
    import os

    from argos.channels.matrix.bridge import BridgeConfig, MatrixBridge
    from argos.channels.matrix.client import MatrixClient

    cfg = load_config()
    mc = cfg.matrix
    token = os.environ.get(mc.token_env, "")
    missing = [
        n
        for n, v in (
            ("matrix.homeserver", mc.homeserver),
            ("matrix.allowed_users", mc.allowed_users),
            (mc.token_env, token),
        )
        if not v
    ]
    if missing:
        console.print(
            f"[red]Falta configuración:[/] {', '.join(missing)} "
            "(config/argos.yaml y secrets/matrix.env)"
        )
        raise typer.Exit(2)
    if not cfg.allows_profile(mc.profile):
        raise typer.BadParameter(f"perfil {mc.profile!r} fuera del segmento {cfg.segment!r}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    async def go() -> None:
        matrix = MatrixClient(mc.homeserver, token)
        try:
            async with _client() as core:
                bridge = MatrixBridge(
                    matrix,
                    core,
                    BridgeConfig(
                        allowed_users=mc.allowed_users,
                        profile=mc.profile,
                        notify_room=mc.notify_room,
                        progress_interval_s=mc.progress_interval_s,
                        open_dm=mc.open_dm,
                    ),
                    cfg.data_path / "matrix.db",
                )
                await bridge.run()
        finally:
            await matrix.aclose()

    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


@app.command("matrix-login")
def matrix_login_cmd(
    force: Annotated[
        bool, typer.Option("--force", help="Rehacer login aunque el token valga")
    ] = False,
) -> None:
    """Obtiene el token del bot con las credenciales del inventario (servicio `matrix`: user,
    password y opcionalmente homeserver) y lo guarda en secrets/matrix.env (600). Ni la
    contraseña ni el token se muestran ni pasan por el modelo."""
    import os
    from pathlib import Path

    from argos.channels.matrix.client import MatrixClient, MatrixError, login
    from argos.inventory import load_inventory

    cfg = load_config()
    inv = load_inventory(cfg.root)
    svc = inv.get("matrix")
    user = svc.get("user") or svc.get("user_id") or cfg.matrix.user_id
    password = inv.secret("matrix", "password")
    if not user or not password:
        console.print("[red]Falta user o password en secrets/inventory.yaml (servicio matrix)[/]")
        raise typer.Exit(2)
    homeserver = (
        svc.get("homeserver")
        or svc.get("url")
        or cfg.matrix.homeserver
        or f"https://{str(user).split(':', 1)[1]}"
    )
    env_path = cfg.root / "secrets" / "matrix.env"

    async def valid(token: str) -> str | None:
        client = MatrixClient(homeserver, token)
        try:
            return await client.whoami()
        except (MatrixError, OSError):
            return None
        finally:
            await client.aclose()

    old = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith(f"{cfg.matrix.token_env}="):
                old = line.split("=", 1)[1].strip()
    if old and not force and (who := asyncio.run(valid(old))):
        console.print(
            f"El token de secrets/matrix.env ya es válido para {who}; nada que hacer "
            "(--force para renovarlo)."
        )
        return
    try:
        data = asyncio.run(login(homeserver, str(user), password))
    except (MatrixError, OSError) as exc:
        console.print(f"[red]login fallido:[/] {exc}")
        raise typer.Exit(1) from exc
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"{cfg.matrix.token_env}={data['access_token']}\n")
    Path(env_path).chmod(0o600)
    console.print(
        f"Token guardado en secrets/matrix.env para {data.get('user_id')} "
        f"(dispositivo {data.get('device_id')}) en {homeserver}."
    )


@app.command("osint-env")
def osint_env_cmd() -> None:
    """Copia la url y la api key del backend OSINT (inventario, servicio `osint-mcp`) a
    secrets/osint.env (600), lo único de secrets/ que recibe el contenedor core-osint. La clave no
    se muestra ni pasa por el modelo."""
    import os

    from argos.inventory import load_inventory

    cfg = load_config()
    inv = load_inventory(cfg.root)
    url = inv.get("osint-mcp").get("url")
    key = inv.secret("osint-mcp", "api_key")
    if not url or not key:
        console.print("[red]Falta url o api_key en secrets/inventory.yaml (servicio osint-mcp)[/]")
        raise typer.Exit(2)
    env_path = cfg.root / "secrets" / "osint.env"
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(f"ARGOS_OSINT_URL={url}\nARGOS_OSINT_KEY={key}\n")
    env_path.chmod(0o600)
    console.print(f"Escrito secrets/osint.env (backend {url}). Recrea core-osint para cargarlo.")


# --- memoria (RF-18) -----------------------------------------------------------------------------

memory_app = typer.Typer(
    help="Memoria durable: inspeccionar, editar y borrar (RF-18).", no_args_is_help=True
)
app.add_typer(memory_app, name="memory")

inventory_app = typer.Typer(
    help="Inventario de infraestructura (secrets/inventory.yaml).", no_args_is_help=True
)
app.add_typer(inventory_app, name="inventory")


@inventory_app.command("show")
def inventory_show() -> None:
    """Muestra el inventario sin secretos (solo indica cuáles hay configurados)."""
    from argos.inventory import load_inventory

    view = load_inventory(load_config().root).public_view()
    if not view:
        console.print("[dim]inventario vacío (secrets/inventory.yaml)[/]")
        return
    console.print_json(json.dumps(view, ensure_ascii=False))


@inventory_app.command("set")
def inventory_set(
    service: str,
    field: str,
    value: Annotated[str | None, typer.Argument(help="Valor; omitir si es secreto")] = None,
) -> None:
    """Fija un campo del inventario. Para campos secretos (api_key, token, password…) omite el
    valor y se pedirá por un prompt oculto: la clave no pasa por el chat ni por la auditoría."""
    from argos.inventory_edit import is_secret, set_field

    if is_secret(field):
        if value is not None:
            console.print(
                "[red]No pases secretos como argumento (quedan en el historial de "
                "shell). Omite el valor y se pedirá de forma oculta.[/]"
            )
            raise typer.Exit(2)
        value = typer.prompt(f"{service}.{field}", hide_input=True, confirmation_prompt=True)
    elif value is None:
        raise typer.BadParameter("indica el valor para un campo no secreto")
    set_field(load_config().root, service, field, value)
    shown = "········" if is_secret(field) else value
    console.print(f"[green]guardado[/] {service}.{field} = {shown} (secrets/inventory.yaml)")


def _state():
    from argos.state import StateStore

    return StateStore(load_config().data_path / "state.db")


@memory_app.command("list")
def memory_list(
    profile: Annotated[str | None, typer.Option("--profile", "-p")] = None,
    query: Annotated[str | None, typer.Option("--query", "-q", help="Buscar")] = None,
) -> None:
    """Lista la memoria (o busca con --query). «tú» = fiable; «agente» = entra como dato."""
    st = _state()
    items = st.search(profile or "personal", query, 30) if query else st.memories(profile)
    table = Table("id", "perfil", "tipo", "origen", "📌", "contenido", "actualizada")
    for m in items:
        table.add_row(
            m.id,
            m.profile,
            m.kind,
            "tú" if m.provenance == "user" else "agente",
            "sí" if m.pinned else "",
            m.content,
            m.updated_at[:16],
        )
    console.print(table)


@memory_app.command("add")
def memory_add(
    content: str,
    profile: Annotated[str, typer.Option("--profile", "-p")] = "personal",
    kind: Annotated[str, typer.Option(help="fact | preference | finding | note")] = "preference",
    pin: Annotated[bool, typer.Option("--pin", help="Inyectar siempre")] = False,
) -> None:
    """Añade una memoria tuya (fiable: entra como preferencia, no como dato)."""
    m = _state().add_memory(profile, kind, content, "user", pinned=pin)
    console.print(f"[green]guardada {m.id}[/]")


@memory_app.command("edit")
def memory_edit(memory_id: str, content: str) -> None:
    """Corrige una memoria; al editarla pasa a ser tuya (revisada)."""
    m = _state().update_memory(memory_id, content=content)
    console.print(f"[green]{m.id} actualizada[/]")


@memory_app.command("pin")
def memory_pin(memory_id: str, off: Annotated[bool, typer.Option("--off")] = False) -> None:
    """Fija (o desfija) una memoria: se inyecta en todas las sesiones del perfil."""
    _state().update_memory(memory_id, pinned=not off)
    console.print("[green]hecho[/]")


@memory_app.command("forget")
def memory_forget(memory_id: str) -> None:
    """Borra una memoria."""
    console.print("[green]olvidada[/]" if _state().forget(memory_id) else "[red]no existe[/]")


@core_app.command("submit")
def core_submit(
    task: Annotated[str, typer.Argument()],
    profile: Annotated[str, typer.Option("--profile", "-p")] = "personal",
    dry_run: Annotated[bool | None, typer.Option("--dry-run/--no-dry-run")] = None,
    budget: Annotated[int | None, typer.Option()] = None,
    follow: Annotated[bool, typer.Option("--follow/--detach")] = True,
) -> None:
    """Envía una tarea al núcleo persistente; por defecto sigue su progreso."""

    async def go() -> None:
        async with _client() as client:
            sid = await client.submit(
                task=task, profile=profile, dry_run=dry_run, budget_tokens=budget, channel="cli"
            )
        console.print(f"sesión [bold]{sid}[/]")
        if follow:
            status = await _attach(sid)
            raise typer.Exit(0 if status == "completed" else 1)

    asyncio.run(go())


@core_app.command("attach")
def core_attach(session: str) -> None:
    """Sigue una sesión en curso (o reproduce una terminada) y responde sus aprobaciones."""
    asyncio.run(_attach(session))


@core_app.command("status")
def core_status() -> None:
    """Salud del núcleo, sesiones en curso, aprobaciones pendientes y tareas programadas."""

    async def go() -> None:
        async with _client() as client:
            health, sessions, pending, schedules = await asyncio.gather(
                client.health(), client.sessions(), client.approvals(), client.schedules()
            )
        console.print_json(json.dumps(health))
        live = [s for s in sessions if s["live"]]
        if live:
            t = Table("sesión", "perfil", "canal", "tarea")
            for s in live:
                t.add_row(s["id"][:12], s["profile"], s["channel"], (s["task"] or "")[:60])
            console.print(t)
        for a in pending:
            console.print(
                f"[yellow]pendiente {a['id']}[/] {a['action']} ({a['risk_class']}) "
                f"sesión {a['session_id'][:12]}"
            )
        if schedules:
            t = Table("tarea", "cron", "activa", "última", "estado", "omitidas")
            for sc in schedules:
                t.add_row(
                    sc["name"],
                    sc["cron"],
                    str(sc["enabled"]),
                    sc["last_fired"] or "-",
                    sc["last_status"] or "-",
                    str(sc["skipped"]),
                )
            console.print(t)

    asyncio.run(go())


@core_app.command("approve")
def core_approve(
    approval_id: str,
    deny: Annotated[bool, typer.Option("--deny", help="Denegar en lugar de aprobar")] = False,
) -> None:
    """Responde a una aprobación pendiente (desde cualquier terminal, RF-20)."""

    async def go() -> bool:
        async with _client() as client:
            return await client.decide(
                approval_id, "denied" if deny else "approved", "cli-user", "cli"
            )

    ok = asyncio.run(go())
    console.print("[green]registrada[/]" if ok else "[red]no existe o ya resuelta[/]")


@core_app.command("cancel")
def core_cancel(session: str) -> None:
    """Cancela una sesión en curso."""

    async def go() -> bool:
        async with _client() as client:
            return await client.cancel(session)

    console.print("[green]cancelada[/]" if asyncio.run(go()) else "[red]no está en curso[/]")


@core_app.command("run-schedule")
def core_run_schedule(name: str) -> None:
    """Dispara ahora una tarea programada (misma política: sin aprobador humano)."""

    async def go() -> dict:
        async with _client() as client:
            return await client.run_schedule(name)

    console.print(asyncio.run(go()))


def _render_tools(items: list[dict]) -> Table:
    table = Table("tool", "versión", "riesgo", "idempotente", "origen", "descripción")
    for t in items:
        table.add_row(
            t["name"],
            t["version"],
            t["risk"],
            str(t["idempotent"]),
            t.get("mcp_server") or "núcleo",
            t["description"],
        )
    return table


@app.command()
def tools(profile: Annotated[str, typer.Option("--profile", "-p")] = "personal") -> None:
    """Catálogo completo de tools del perfil, incluidas las MCP y las skills (RF-11)."""
    from argos.core.session import catalog

    cfg = load_config()
    console.print(_render_tools(asyncio.run(catalog(cfg, profile))))


@app.command()
def kill(reason: Annotated[str, typer.Argument()] = "manual") -> None:
    """Kill switch: detiene todas las sesiones y bloquea nuevas (RF-GOV-02)."""
    _, store = _ctx()
    KillSwitch(store).engage(reason)
    console.print(f"[bold red]KILL SWITCH ACTIVADO[/] ({reason}). Rearmar con `argos rearm`.")


@app.command()
def rearm() -> None:
    """Rearma el sistema tras un kill switch."""
    _, store = _ctx()
    KillSwitch(store).rearm()
    console.print("[green]Sistema rearmado.[/]")


@app.command()
def purge(dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    """Purga sesiones fuera de retención del segmento activo (RF-LEG-03, RF-OB-13)."""
    cfg, store = _ctx()
    report = retention.purge(cfg, store, dry_run=dry_run)
    verb = "Se purgarían" if dry_run else "Purgadas"
    console.print(
        f"{verb} {len(report.sessions)} sesiones, {report.memories} memorias y "
        f"{report.blobs} blobs huérfanos ({report.bytes_freed / 1e6:.1f} MB) en el "
        f"segmento {cfg.segment}."
    )


@app.command()
def backup(no_encrypt: Annotated[bool, typer.Option("--no-encrypt")] = False) -> None:
    """Copia de seguridad cifrada con age de la auditoría del segmento (RNF-10, RF-LEG-04)."""
    cfg, store = _ctx()
    try:
        path = retention.backup(cfg, store, encrypt=not no_encrypt)
    except retention.BackupError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Backup:[/] {path}")


# --- auditoría ---------------------------------------------------------------------------------


@audit_app.command("list")
def audit_list(limit: int = 20) -> None:
    """Últimas sesiones."""
    _, store = _ctx()
    table = Table("sesión", "inicio", "perfil", "canal", "estado", "modelo", "tarea")
    for s in store.sessions(limit):
        table.add_row(
            s["id"][:12],
            s["started_at"][:19],
            s["profile"],
            s["channel"],
            s["status"],
            s["model"],
            (s["task"] or "")[:60],
        )
    console.print(table)


@audit_app.command("show")
def audit_show(session: str) -> None:
    """Resumen de una sesión con las versiones que la produjeron (CA-7)."""
    _, store = _ctx()
    s = summarize(store, store.resolve_session(session))
    console.print_json(
        json.dumps(
            {
                "session": s.session_id,
                "status": s.status,
                "profile": s.profile,
                "versions": s.versions,
                "steps": s.steps,
                "tokens": s.tokens,
                "cost_equiv_usd": s.cost,
                "tool_calls": s.tool_calls,
                "tool_errors": s.tool_errors,
                "shell_execs": s.shell_execs,
                "package_installs": s.installs,
                "errors": dict(s.errors),
                "duration_s": s.duration_s,
            },
            default=str,
        )
    )


@audit_app.command("replay")
def audit_replay(
    session: str,
    step: Annotated[bool, typer.Option("--step", help="Pausa entre turnos")] = False,
    full: Annotated[bool, typer.Option("--full", help="Incluye stdout/stderr")] = False,
) -> None:
    """Reproduce una sesión paso a paso desde la auditoría (RF-OB-04, CA-3)."""
    _, store = _ctx()
    for block in replay_lines(store, store.resolve_session(session), full):
        for line in block:
            console.print(line, highlight=False)
        if step:
            typer.prompt("", default="", show_default=False, prompt_suffix="[enter] ")


@audit_app.command("cost")
def audit_cost(session: str) -> None:
    """Tokens y coste por turno y total (CA-4, RF-CTX-08)."""
    _, store = _ctx()
    s = summarize(store, store.resolve_session(session))
    table = Table(
        "turno",
        "ruta",
        "modelo",
        "prompt",
        "completion",
        "cached",
        "contexto (chars)",
        "coste eq. USD",
    )
    for t in s.per_turn:
        seq = str(t["seq"]) + (" (int)" if t["purpose"] == "internal" else "")
        table.add_row(
            seq,
            t["route"] or "-",
            t["model"],
            str(t["prompt"]),
            str(t["completion"]),
            str(t["cached"]),
            str(t["context_chars"]),
            f"{t['cost']:.6f}",
        )
    table.add_row(
        "[bold]total",
        "",
        "",
        str(s.prompt_tokens),
        str(s.completion_tokens),
        str(s.cached_tokens),
        "",
        f"[bold]{s.cost:.6f}",
    )
    console.print(table)


@audit_app.command("diff")
def audit_diff(a: str, b: str) -> None:
    """Compara dos ejecuciones: versiones, métricas y secuencia de acciones (RF-OB-05)."""
    _, store = _ctx()
    d = diff_sessions(store, store.resolve_session(a), store.resolve_session(b))
    console.print("[bold]Versiones que difieren[/]")
    for k, (va, vb) in d["versions"].items() or {}:
        console.print(f"  {k}: {va} → {vb}")
    if not d["versions"]:
        console.print("  (idénticas)")
    table = Table("métrica", a[:8], b[:8])
    for k, (va, vb) in d["metrics"].items():
        table.add_row(k, str(va), str(vb))
    for k, (va, vb) in d["errors"].items():
        table.add_row(f"error:{k}", str(va), str(vb))
    console.print(table)
    console.print("[bold]Secuencia de acciones[/]")
    console.print("\n".join(d["actions"]) or "  (idéntica)", highlight=False, markup=False)


@audit_app.command("metrics")
def audit_metrics() -> None:
    """Métricas agregadas por perfil, tool y tipo de error (RF-OB-06)."""
    _, store = _ctx()
    m = aggregate_metrics(store)
    t1 = Table("perfil", "sesiones", "tasa éxito", "tokens", "coste eq.", "duración media s")
    for name, p in m["profiles"].items():
        n = p["sessions"] or 1
        t1.add_row(
            name,
            str(int(p["sessions"])),
            f"{p['completed'] / n:.0%}",
            str(int(p["tokens"])),
            f"{p['cost']:.4f}",
            f"{p['duration_s'] / n:.1f}",
        )
    console.print(t1)
    t2 = Table("tool", "llamadas", "tasa error", "reintentos", "duración media ms")
    for name, t in m["tools"].items():
        n = t["calls"] or 1
        t2.add_row(
            name,
            str(int(t["calls"])),
            f"{t['errors'] / n:.0%}",
            str(int(t["retries"])),
            f"{t['duration_ms'] / n:.0f}",
        )
    console.print(t2)
    console.print({"errores": m["errors"]})


@audit_app.command("feedback")
def audit_feedback(
    session: str,
    rating: Annotated[int, typer.Option(min=1, max=5)],
    correction: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Registra feedback humano sobre una sesión (RF-OB-07)."""
    _, store = _ctx()
    sid = store.resolve_session(session)
    store.emit(
        Feedback(session_id=sid, trace_id=sid, rating=rating, correction=correction, source="human")
    )
    console.print("[green]Feedback registrado.[/]")


# --- skills ------------------------------------------------------------------------------------


@skills_app.command("list")
def skills_list() -> None:
    """Catálogo de skills con versión (RF-SK-03, RF-SK-07)."""
    cfg = load_config()
    reg = SkillRegistry(cfg.root / "skills")
    table = Table("skill", "versión", "descripción")
    for sk in reg.skills.values():
        table.add_row(sk.name, sk.full_version, sk.description)
    console.print(table)
    for err in reg.errors:
        console.print(f"[red]inválida:[/] {err}")


@skills_app.command("show")
def skills_show(name: str) -> None:
    """Muestra el contenido de una skill."""
    cfg = load_config()
    sk = SkillRegistry(cfg.root / "skills").skills.get(name)
    if not sk:
        raise typer.BadParameter(f"skill desconocida: {name}")
    console.print(f"[bold]{sk.name}[/] {sk.full_version} — {sk.description}\n")
    console.print(sk.body, markup=False, highlight=False)


@skills_app.command("install")
def skills_install(
    source: Annotated[str, typer.Argument(help="Directorio local o URL git")],
    force: Annotated[bool, typer.Option("--force", help="Sobrescribe si existe")] = False,
) -> None:
    """Instala o actualiza una skill desde un origen de confianza (RF-SK-04)."""
    cfg = load_config()
    try:
        sk = SkillRegistry(cfg.root / "skills").install(source, force)
    except SkillError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Instalada {sk.name} {sk.full_version}[/]")


# --- evaluación --------------------------------------------------------------------------------


def _parse_sets(values: list[str] | None) -> dict[str, object]:
    """`clave.anidada=valor` (valor en YAML) → overrides de configuración."""
    out: dict[str, object] = {}
    for item in values or []:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--set espera clave=valor: {item!r}")
        out[key.strip()] = yaml.safe_load(raw)
    return out


@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Argument()] = "golden",
    provider: Annotated[str, typer.Option(help="fake (CI) | codex")] = "fake",
    task: Annotated[list[str] | None, typer.Option(help="Solo estas tareas")] = None,
    repeat: Annotated[int, typer.Option(min=1, help="Corridas por tarea (modelos reales)")] = 1,
    set_: Annotated[
        list[str] | None,
        typer.Option(
            "--set", help="Override de config para A/B, p. ej. model.routes.decide.model=gpt-6-luna"
        ),
    ] = None,
    baseline: Annotated[Path | None, typer.Option(help="JSON de una corrida previa")] = None,
    save_baseline: Annotated[bool, typer.Option(help="Guarda como evals/baselines/")] = False,
) -> None:
    """Ejecuta una suite de tareas doradas; con --baseline actúa como puerta de regresión."""
    overrides = _parse_sets(set_)
    cfg = load_config(overrides=overrides)
    store = AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))
    summary = asyncio.run(
        run_suite(
            cfg,
            store,
            suite,
            provider,
            lambda: make_provider(cfg, provider),
            task,
            baseline_ref=str(baseline) if baseline else None,
            repeat=repeat,
            overrides=overrides,
        )
    )
    table = Table("tarea", "run", "estado", "score", "pasos", "tokens", "sesión", "detalle")
    for t in summary["tasks"]:
        failed = [c["detail"] for c in t["checks"] if not c["passed"] and c["required"]]
        color = {"passed": "green", "failed": "red", "skipped": "yellow"}.get(t["status"], "red")
        table.add_row(
            t["task_id"],
            str(t["metrics"].get("run", "")),
            f"[{color}]{t['status']}",
            f"{t['score']:.2f}",
            str(t["metrics"].get("steps", "")),
            str(t["metrics"].get("tokens", "")),
            (t["session_id"] or "")[:12],
            t["reason"] or "; ".join(failed)[:80],
        )
    console.print(table)
    console.print(summary["aggregate"])
    console.print(f"[dim]resultado: {cfg.data_path / 'evals' / (summary['run_id'] + '.json')}[/]")
    if save_baseline:
        path = cfg.root / "evals" / "baselines" / f"{suite}-{provider}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2))
        console.print(f"[green]Línea base guardada en {path}[/]")
    if baseline:
        regressions = compare_to_baseline(summary, json.loads(baseline.read_text()))
        if regressions:
            console.print("[bold red]REGRESIONES:[/]\n  " + "\n  ".join(regressions))
            raise typer.Exit(1)
        console.print("[green]Sin regresiones frente a la línea base.[/]")
    if any(t["status"] in ("failed", "error") for t in summary["tasks"]):
        raise typer.Exit(1)


@eval_app.command("compare")
def eval_compare(a: Path, b: Path) -> None:
    """Compara dos corridas de evaluación (A/B, RF-EV-04)."""
    ra, rb = json.loads(a.read_text()), json.loads(b.read_text())
    for label, r in (("A", ra), ("B", rb)):
        console.print(
            f"{label}: {r['run_id']} overrides={r.get('overrides', {})} config={r['config_hash']}"
        )
    table = Table("tarea", "éxito A", "éxito B", "tokens A", "tokens B", "pasos A", "pasos B")
    for tid, x, y in compare_runs(ra, rb):

        def pct(m):
            return f"{m['pass_rate']:.0%}" if m else "-"

        table.add_row(
            tid,
            pct(x),
            pct(y),
            str(x.get("mean_tokens", "-")),
            str(y.get("mean_tokens", "-")),
            str(x.get("mean_steps", "-")),
            str(y.get("mean_steps", "-")),
        )
    console.print(table)
    console.print({"A": ra["aggregate"], "B": rb["aggregate"]})


if __name__ == "__main__":
    app()
