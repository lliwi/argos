"""Tool `shell.exec`: ejecuta un comando en el sandbox de la sesión (§8).

- Riesgo por comando: patrones destructivos u ofensivos elevan la clase (RF-EX-07, RF-21).
- Herramientas ofensivas solo contra objetivos en `scope` del perfil (RF-SEC-06).
- Registra `shell_exec`, `package_install` y bloqueos de egress (RF-EX-06, CA-2, CA-6).
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from argos.audit.events import ErrorEvent, ErrorKind, PackageInstall, RiskClass, ShellExec
from argos.tools.base import Tool, ToolContext, ToolError, ToolResult

DESTRUCTIVE = [
    re.compile(p) for p in (
        r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/",  # rm -rf sobre rutas absolutas
        r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r",
        r"\bmkfs(\.\w+)?\b", r"\bdd\s+.*\bof=/dev/", r">\s*/dev/sd[a-z]",
        r"\bshutdown\b|\breboot\b|\bpoweroff\b", r":\(\)\s*\{\s*:\|:&\s*\};:",
        r"\bchmod\s+-R\s+0?777\s+/", r"\bgit\s+push\s+.*--force\b",
    )
]
OFFENSIVE_TOOLS = {
    "nmap", "masscan", "sqlmap", "nikto", "hydra", "medusa", "wpscan", "gobuster", "ffuf",
    "dirb", "nuclei", "metasploit", "msfconsole", "john", "hashcat", "responder",
}

INSTALL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("pip", re.compile(r"\b(?:pip3?|python3?\s+-m\s+pip|uv\s+pip)\s+install\s+([^;&|]+)")),
    ("npm", re.compile(r"\bnpm\s+(?:install|i|add)\s+([^;&|]+)")),
    ("yarn", re.compile(r"\byarn\s+add\s+([^;&|]+)")),
    ("cargo", re.compile(r"\bcargo\s+install\s+([^;&|]+)")),
    ("apt", re.compile(r"\bapt(?:-get)?\s+install\s+([^;&|]+)")),
]
# Herramienta en posición de comando: inicio, tras ; & | ( ` o comillas (bash -c "..."), o tras
# envoltorios típicos. Heurística: la contención real es la allowlist de egress + el scope.
_OFFENSIVE_CMD = re.compile(
    r"(?:^|[;&|(`\"']\s*|\b(?:sudo|env|nohup|exec|xargs|timeout\s+\S+)\s+)(?:\S*/)?(?:"
    + "|".join(sorted(OFFENSIVE_TOOLS)) + r")(?=\s|$|[;&|\"'])")
_HOSTLIKE = re.compile(
    r"(?:https?://)?((?:\d{1,3}\.){3}\d{1,3}(?:/\d+)?|(?:[a-z0-9-]+\.)+[a-z]{2,})", re.I)


def parse_installs(command: str) -> list[tuple[str, str, str | None]]:
    """Devuelve [(gestor, paquete, versión)] declarados en el comando."""
    found = []
    for manager, pat in INSTALL_PATTERNS:
        for match in pat.finditer(command):
            try:
                tokens = shlex.split(match.group(1))
            except ValueError:
                tokens = match.group(1).split()
            for tok in tokens:
                if tok.startswith("-") or tok in (">", "2>&1"):
                    continue
                found.append((manager, *_split_version(tok)))
    return found


_VERSION_SEP = re.compile(r"==|>=|<=|~=|=|(?<=.)@")


def _split_version(tok: str) -> tuple[str, str | None]:
    """`pandas==2.2` → (pandas, 2.2); `@scope/pkg@1.0` → (@scope/pkg, 1.0)."""
    parts = _VERSION_SEP.split(tok, maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (tok, None)


def offensive_targets(command: str) -> list[str] | None:
    """Si el comando usa una herramienta ofensiva, devuelve los objetivos detectados."""
    if not _OFFENSIVE_CMD.search(command):
        return None
    return [m.group(1) for m in _HOSTLIKE.finditer(command)]


class ShellExecTool(Tool):
    name = "shell.exec"
    version = "0.1.0"
    description = (
        "Ejecuta un comando bash en el sandbox Linux de la sesión (cwd /workspace/out). "
        "Devuelve exit_code, stdout y stderr.")
    parameters = {
        "type": "object", "required": ["command"],
        "properties": {
            "command": {"type": "string"},
            "timeout_s": {"type": "integer", "description": "opcional, máx. 600"},
        },
    }
    risk_class = RiskClass.WRITE
    idempotent = False

    def risk_for(self, args: dict[str, Any]) -> RiskClass:
        command = str(args.get("command", ""))
        if offensive_targets(command) is not None:
            return RiskClass.OFFENSIVE
        if any(p.search(command) for p in DESTRUCTIVE):
            return RiskClass.DESTRUCTIVE
        return RiskClass.WRITE

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            raise ToolError("command vacío", ErrorKind.VALIDATION_ERROR)
        targets = offensive_targets(command)
        if targets is not None:
            out_of_scope = [t for t in targets if t not in ctx.profile.scope]
            if not targets or out_of_scope:
                raise ToolError(
                    f"objetivo(s) fuera de alcance autorizado: {out_of_scope or '(sin objetivo)'}"
                    f" — scope del perfil: {ctx.profile.scope}", ErrorKind.VALIDATION_ERROR)

        if ctx.dry_run:
            ctx.emit(ShellExec(session_id=ctx.session_id, turn_id=ctx.turn_id, command=command,
                               cwd="/workspace/out", exit_code=None, dry_run=True))
            return ToolResult(f"[dry-run] se ejecutaría: {command}", data={"dry_run": True})

        if ctx.sandbox is None:
            raise ToolError("no hay sandbox disponible", ErrorKind.SANDBOX_ERROR)
        timeout = min(int(args.get("timeout_s") or 0) or 0, 600) or None
        try:
            res = await ctx.sandbox.exec(command, timeout)
        except Exception as exc:  # noqa: BLE001 — cualquier fallo del sandbox es sandbox_error
            raise ToolError(f"fallo del sandbox: {exc}", ErrorKind.SANDBOX_ERROR) from exc

        stdout_ref = ctx.store.put_blob(res.stdout) if res.stdout else None
        stderr_ref = ctx.store.put_blob(res.stderr) if res.stderr else None
        ctx.emit(ShellExec(
            session_id=ctx.session_id, turn_id=ctx.turn_id, command=command, cwd="/workspace/out",
            exit_code=res.exit_code, stdout_ref=stdout_ref, stderr_ref=stderr_ref,
            duration_ms=res.duration_ms, sandbox_id=ctx.sandbox.id))

        for manager, package, version in parse_installs(command):
            ctx.emit(PackageInstall(
                session_id=ctx.session_id, turn_id=ctx.turn_id, manager=manager,
                package=package, version=version, status="ok" if res.exit_code == 0 else "error"))

        blocked = ctx.sandbox.egress_blocked_since_last()
        for b in blocked:
            ctx.emit(ErrorEvent(session_id=ctx.session_id, turn_id=ctx.turn_id,
                                kind=ErrorKind.EGRESS_BLOCKED,
                                message=f"{b.host}:{b.port} — {b.reason}"))

        output = f"exit_code={res.exit_code}\n--- stdout ---\n{res.stdout}"
        if res.stderr:
            output += f"\n--- stderr ---\n{res.stderr}"
        if blocked:
            output += "\n--- egress bloqueado ---\n" + "\n".join(
                f"{b.host}: {b.reason}" for b in blocked)
        if res.timed_out:
            return ToolResult(output + "\n(timeout)", ok=False, error_kind=ErrorKind.TIMEOUT)
        # Un exit != 0 es una observación válida, no un fallo de la tool: el agente decide.
        return ToolResult(output, data={"exit_code": res.exit_code,
                                        "egress_blocked": [b.host for b in blocked]})
