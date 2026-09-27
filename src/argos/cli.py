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
from argos.core.session import SessionOptions, SessionRefused, build_tools, run_session
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
    opts = SessionOptions(task=task, profile=profile, channel="cli", dry_run=dry_run,
                          allow_domains=allow_domain or [], session_budget_tokens=budget)
    try:
        result = asyncio.run(run_session(
            opts, cfg, make_provider(cfg, provider), store=store, approver=cli_approver(),
            on_progress=progress_printer(verbose=not quiet)))
    except SessionRefused as exc:
        console.print(f"[red]Sesión rechazada:[/] {exc}")
        raise typer.Exit(2) from exc
    color = "green" if result.status == "completed" else "red"
    console.print(f"\n[{color}]{result.status}[/] · sesión {result.session_id} · "
                  f"{result.steps} pasos · {result.tokens} tokens · workspace {result.workspace}")
    if result.status != "completed":
        console.print(f"[{color}]{result.message}[/]")
        raise typer.Exit(1)


# --- núcleo persistente (RF-04) -----------------------------------------------------------------

core_app = typer.Typer(help="Núcleo persistente: API, sesiones en segundo plano, scheduler.",
                       no_args_is_help=True)
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
    console.print(f"[green]Argos[/] segmento [bold]{cfg.segment}[/] · API {cfg.api_socket}"
                  + (f" · webhooks {hooks_host}:{hooks_port}" if hooks_port else "")
                  + f" · {len(core.scheduler.sched.schedules)} tareas programadas")
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


async def _attach(sid: str, interactive: bool = True, compact: bool = False) -> str:
    """Muestra el progreso en vivo y resuelve aprobaciones desde la terminal (canal CLI)."""
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
                console.print(f"\n[bold yellow]APROBACIÓN {data['id']}[/] {data['action']} "
                              f"(riesgo {data['risk_class']}, {data['timeout_s']}s)")
                console.print(data["details"], markup=False, highlight=False)
                if not (interactive and sys.stdin.isatty()):
                    console.print(f"  responde con: argos core approve {data['id']} [--deny]")
                    continue
                try:
                    answer = await asyncio.wait_for(asyncio.to_thread(
                        input, "¿Aprobar? [s/N] "), data["timeout_s"])
                except TimeoutError:
                    continue
                decision = "approved" if answer.strip().lower() in ("s", "si", "sí", "y") \
                    else "denied"
                await client.decide(data["id"], decision, "cli-user", "cli")
                continue
            try:
                event = parse_event(data)
            except (KeyError, ValueError):
                continue
            if compact and data.get("type") == "session":
                # En el chat basta con saber que arrancó; versiones y tools, en `audit show`.
                if data.get("parent_session_id"):
                    console.print(f"[dim]  ↳ subagente {data['session_id'][:8]}: "
                                  f"{data['task'][:80]}[/]")
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
    console.print("[bold]Argos · consola[/]" + (
        f" · Herdr pane {reporter.pane}" if reporter.enabled else " · (sin Herdr)"))

    async def go() -> None:
        async with _client() as client:
            await run_console(client, reporter, console)
    try:
        asyncio.run(go())
    except KeyboardInterrupt:
        pass


@app.command("chat")
def chat_cmd(profile: Annotated[str, typer.Option("--profile", "-p")] = "personal") -> None:
    """Conversación con el núcleo persistente: escribe una tarea, sigue su progreso y responde
    sus aprobaciones; al terminar, la siguiente. Ctrl-D para salir."""
    console.print(f"[bold]Argos[/] · perfil {profile} · escribe una tarea (Ctrl-D para salir)")

    async def one(task: str) -> None:
        async with _client() as client:
            sid = await client.submit(task=task, profile=profile, channel="chat")
        console.print(f"[dim]sesión {sid[:12]}[/]")
        await _attach(sid, compact=True)

    while True:
        try:
            task = input("\nargos> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not task:
            continue
        try:
            asyncio.run(one(task))
        except KeyboardInterrupt:
            console.print("[yellow]desconectado (la sesión sigue en el núcleo; "
                          "`argos core attach` para retomarla)[/]")
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/]")


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
            sid = await client.submit(task=task, profile=profile, dry_run=dry_run,
                                      budget_tokens=budget, channel="cli")
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
                client.health(), client.sessions(), client.approvals(), client.schedules())
        console.print_json(json.dumps(health))
        live = [s for s in sessions if s["live"]]
        if live:
            t = Table("sesión", "perfil", "canal", "tarea")
            for s in live:
                t.add_row(s["id"][:12], s["profile"], s["channel"], (s["task"] or "")[:60])
            console.print(t)
        for a in pending:
            console.print(f"[yellow]pendiente {a['id']}[/] {a['action']} ({a['risk_class']}) "
                          f"sesión {a['session_id'][:12]}")
        if schedules:
            t = Table("tarea", "cron", "activa", "última", "estado", "omitidas")
            for sc in schedules:
                t.add_row(sc["name"], sc["cron"], str(sc["enabled"]), sc["last_fired"] or "-",
                          sc["last_status"] or "-", str(sc["skipped"]))
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
            return await client.decide(approval_id, "denied" if deny else "approved",
                                       "cli-user", "cli")
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


@app.command()
def tools(profile: Annotated[str | None, typer.Option("--profile", "-p")] = None) -> None:
    """Catálogo de tools nativas (RF-11). Las MCP se listan al conectar en sesión."""
    cfg = load_config()
    reg = build_tools(cfg)
    if profile:
        reg = reg.for_profile(cfg.profile(profile))
    table = Table("tool", "versión", "riesgo", "idempotente", "descripción")
    for t in reg:
        table.add_row(t.name, t.version, t.risk_class.value, str(t.idempotent), t.description)
    console.print(table)


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
    console.print(f"{verb} {len(report.sessions)} sesiones y {report.blobs} blobs huérfanos "
                  f"({report.bytes_freed / 1e6:.1f} MB) en el segmento {cfg.segment}.")


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
        table.add_row(s["id"][:12], s["started_at"][:19], s["profile"], s["channel"],
                      s["status"], s["model"], (s["task"] or "")[:60])
    console.print(table)


@audit_app.command("show")
def audit_show(session: str) -> None:
    """Resumen de una sesión con las versiones que la produjeron (CA-7)."""
    _, store = _ctx()
    s = summarize(store, store.resolve_session(session))
    console.print_json(json.dumps({
        "session": s.session_id, "status": s.status, "profile": s.profile,
        "versions": s.versions, "steps": s.steps, "tokens": s.tokens, "cost_equiv_usd": s.cost,
        "tool_calls": s.tool_calls, "tool_errors": s.tool_errors, "shell_execs": s.shell_execs,
        "package_installs": s.installs, "errors": dict(s.errors), "duration_s": s.duration_s,
    }, default=str))


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
    table = Table("turno", "ruta", "modelo", "prompt", "completion", "cached",
                  "contexto (chars)", "coste eq. USD")
    for t in s.per_turn:
        seq = str(t["seq"]) + (" (int)" if t["purpose"] == "internal" else "")
        table.add_row(seq, t["route"] or "-", t["model"], str(t["prompt"]), str(t["completion"]),
                      str(t["cached"]), str(t["context_chars"]), f"{t['cost']:.6f}")
    table.add_row("[bold]total", "", "", str(s.prompt_tokens), str(s.completion_tokens),
                  str(s.cached_tokens), "", f"[bold]{s.cost:.6f}")
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
        t1.add_row(name, str(int(p["sessions"])), f"{p['completed'] / n:.0%}",
                   str(int(p["tokens"])), f"{p['cost']:.4f}", f"{p['duration_s'] / n:.1f}")
    console.print(t1)
    t2 = Table("tool", "llamadas", "tasa error", "reintentos", "duración media ms")
    for name, t in m["tools"].items():
        n = t["calls"] or 1
        t2.add_row(name, str(int(t["calls"])), f"{t['errors'] / n:.0%}", str(int(t["retries"])),
                   f"{t['duration_ms'] / n:.0f}")
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
    store.emit(Feedback(session_id=sid, trace_id=sid, rating=rating, correction=correction,
                        source="human"))
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
    set_: Annotated[list[str] | None, typer.Option(
        "--set", help="Override de config para A/B, p. ej. model.routes.decide.model=gpt-6-luna")
    ] = None,
    baseline: Annotated[Path | None, typer.Option(help="JSON de una corrida previa")] = None,
    save_baseline: Annotated[bool, typer.Option(help="Guarda como evals/baselines/")] = False,
) -> None:
    """Ejecuta una suite de tareas doradas; con --baseline actúa como puerta de regresión."""
    overrides = _parse_sets(set_)
    cfg = load_config(overrides=overrides)
    store = AuditStore(cfg.data_path, Redactor(cfg.audit.redact_pii))
    summary = asyncio.run(run_suite(
        cfg, store, suite, provider, lambda: make_provider(cfg, provider), task,
        baseline_ref=str(baseline) if baseline else None, repeat=repeat, overrides=overrides))
    table = Table("tarea", "run", "estado", "score", "pasos", "tokens", "sesión", "detalle")
    for t in summary["tasks"]:
        failed = [c["detail"] for c in t["checks"] if not c["passed"] and c["required"]]
        color = {"passed": "green", "failed": "red", "skipped": "yellow"}.get(t["status"], "red")
        table.add_row(t["task_id"], str(t["metrics"].get("run", "")), f"[{color}]{t['status']}",
                      f"{t['score']:.2f}", str(t["metrics"].get("steps", "")),
                      str(t["metrics"].get("tokens", "")), (t["session_id"] or "")[:12],
                      t["reason"] or "; ".join(failed)[:80])
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
        console.print(f"{label}: {r['run_id']} overrides={r.get('overrides', {})} "
                      f"config={r['config_hash']}")
    table = Table("tarea", "éxito A", "éxito B", "tokens A", "tokens B", "pasos A", "pasos B")
    for tid, x, y in compare_runs(ra, rb):
        def pct(m):
            return f"{m['pass_rate']:.0%}" if m else "-"
        table.add_row(tid, pct(x), pct(y), str(x.get("mean_tokens", "-")),
                      str(y.get("mean_tokens", "-")), str(x.get("mean_steps", "-")),
                      str(y.get("mean_steps", "-")))
    console.print(table)
    console.print({"A": ra["aggregate"], "B": rb["aggregate"]})


if __name__ == "__main__":
    app()
