"""Proveedor Codex CLI: `codex exec` como paso de completado (ADR-0001).

Codex se ejecuta en un directorio vacío, con sandbox `read-only`, sin config de usuario y sin
persistir sesión. Su salida final está forzada por `--output-schema`. Nosotros solo leemos la
decisión y el uso de tokens del stream `--json`.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from argos.model.base import (
    DECISION_SCHEMA,
    ModelAuthError,
    ModelError,
    ModelRequest,
    ModelResponse,
    Route,
    Usage,
    parse_decision,
)

# Ítems que indican que Codex actuó por su cuenta en lugar de devolver solo una decisión.
ACTION_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def parse_codex_events(lines: list[str]) -> tuple[str, Usage, list[str]]:
    """Extrae (último mensaje del agente, uso, violaciones) de un stream JSONL de `codex exec`.

    Tolerante a eventos desconocidos: el formato no es un contrato estable.
    """
    text = ""
    usage = Usage()
    violations: list[str] = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "turn.failed":
            message = (ev.get("error") or {}).get("message", "turn.failed")
            if "401" in message or "unauthorized" in message.lower():
                raise ModelAuthError(f"{message} — ejecuta `codex login`")
            raise ModelError(message)
        if etype == "item.completed":
            item = ev.get("item") or {}
            itype = item.get("type")
            if itype == "agent_message":
                text = item.get("text", text)
            elif itype in ACTION_ITEM_TYPES:
                violations.append(f"{itype}: {json.dumps(item, ensure_ascii=False)[:300]}")
        elif etype == "turn.completed":
            u = ev.get("usage") or {}
            usage.prompt_tokens += int(u.get("input_tokens", 0))
            usage.cached_tokens += int(u.get("cached_input_tokens", 0))
            usage.completion_tokens += int(u.get("output_tokens", 0))
    return text, usage, violations


class CodexCliProvider:
    """`overrides` son pares `clave=valor` TOML que se pasan como `-c` (p. ej. para recortar el
    prompt base de Codex); `disable_features` apaga features que añaden tools al contexto."""

    def __init__(self, model: str | None = None, timeout_s: int = 300, binary: str = "codex",
                 overrides: dict[str, str] | None = None,
                 disable_features: list[str] | None = None,
                 instructions: str | None = None) -> None:
        self.model = model
        self.timeout_s = timeout_s
        self.binary = binary
        self.overrides = dict(overrides or {})
        self.disable_features = list(disable_features or [])
        # Sustituye el prompt base de Codex (`model_instructions_file`): su mayor sobrecoste fijo.
        self.instructions = instructions
        self.name = f"codex-cli:{model or 'default'}"

    def build_command(self, workdir: Path, schema: Path,
                      route: Route | None = None) -> list[str]:
        cmd = [
            self.binary, "exec", "--json", "--ephemeral", "--ignore-user-config",
            "--skip-git-repo-check", "-s", "read-only", "-C", str(workdir),
            "--output-schema", str(schema), "--color", "never",
        ]
        model = (route.model if route else None) or self.model
        if model:
            cmd += ["-m", model]
        if route and route.effort:
            cmd += ["-c", f"model_reasoning_effort={json.dumps(route.effort)}"]
        if self.instructions is not None:
            path = schema.parent / "instructions.md"
            path.write_text(self.instructions, encoding="utf-8")
            cmd += ["-c", f"model_instructions_file={json.dumps(str(path))}"]
        for key, value in self.overrides.items():
            cmd += ["-c", f"{key}={value}"]
        for feature in self.disable_features:
            cmd += ["--disable", feature]
        cmd.append("-")
        return cmd

    async def complete(self, request: ModelRequest,
                       route: Route | None = None) -> ModelResponse:
        prompt = request.render()
        with tempfile.TemporaryDirectory(prefix="argos-codex-") as tmp:
            workdir = Path(tmp) / "empty"
            workdir.mkdir()
            schema = Path(tmp) / "decision.schema.json"
            schema.write_text(json.dumps(DECISION_SCHEMA))
            cmd = self.build_command(workdir, schema, route)
            start = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError as exc:
                raise ModelError(f"no se encuentra el binario {self.binary!r}") from exc
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self.timeout_s)
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ModelError(f"codex exec superó {self.timeout_s}s") from exc
            latency_ms = int((time.monotonic() - start) * 1000)

        lines = out.decode("utf-8", errors="replace").splitlines()
        text, usage, violations = parse_codex_events(lines)
        if proc.returncode != 0 and not text:
            tail = err.decode("utf-8", errors="replace").strip().splitlines()[-3:]
            raise ModelError(f"codex exec salió con {proc.returncode}: {' | '.join(tail)}")
        return ModelResponse(
            decision=parse_decision(text), usage=usage, latency_ms=latency_ms,
            raw_text=text, violations=violations,
            model=((route.model if route else None) or self.model or "codex-default")
            + (f"@{route.effort}" if route and route.effort else ""))
