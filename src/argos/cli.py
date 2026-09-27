"""CLI `argos`: ejecutar tareas, revisar auditoría, evaluar y gobernar."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from argos import retention
from argos.audit.events import Feedback
from argos.audit.redact import Redactor
from argos.audit.review import aggregate_metrics, diff_sessions, replay_lines, summarize
from argos.audit.store import AuditStore
from argos.channels.cli import cli_approver, console, progress_printer
from argos.config import Config, load_config
from argos.core.session import SessionOptions, SessionRefused, build_tools, run_session
from argos.eval.runner import compare_to_baseline, run_suite
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

@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Argument()] = "golden",
    provider: Annotated[str, typer.Option(help="fake (CI) | codex")] = "fake",
    task: Annotated[list[str] | None, typer.Option(help="Solo estas tareas")] = None,
    baseline: Annotated[Path | None, typer.Option(help="JSON de una corrida previa")] = None,
    save_baseline: Annotated[bool, typer.Option(help="Guarda como evals/baselines/")] = False,
) -> None:
    """Ejecuta una suite de tareas doradas; con --baseline actúa como puerta de regresión."""
    cfg, store = _ctx()
    summary = asyncio.run(run_suite(
        cfg, store, suite, provider, lambda: make_provider(cfg, provider), task,
        baseline_ref=str(baseline) if baseline else None))
    table = Table("tarea", "estado", "score", "pasos", "tokens", "sesión", "detalle")
    for t in summary["tasks"]:
        failed = [c["detail"] for c in t["checks"] if not c["passed"] and c["required"]]
        color = {"passed": "green", "failed": "red", "skipped": "yellow"}.get(t["status"], "red")
        table.add_row(t["task_id"], f"[{color}]{t['status']}", f"{t['score']:.2f}",
                      str(t["metrics"].get("steps", "")), str(t["metrics"].get("tokens", "")),
                      (t["session_id"] or "")[:12], t["reason"] or "; ".join(failed)[:80])
    console.print(table)
    console.print(summary["aggregate"])
    console.print(f"[dim]resultado: var/evals/{summary['run_id']}.json[/]")
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


if __name__ == "__main__":
    app()
