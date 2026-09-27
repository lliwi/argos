"""Mide el sobrecoste fijo de Codex CLI por llamada bajo distintas configuraciones (Fase 2, A).

Uso (en contenedor): scripts/argos-py scripts/bench_codex_overhead.py [variante ...]
Cada variante hace UNA llamada real con el mismo prompt mínimo y reporta tokens y latencia.
Resultado en var/bench/codex-overhead-<ts>.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from argos.config import ROOT
from argos.model.base import Message, ModelRequest, ToolSpec
from argos.model.codex_cli import CodexCliProvider

TOOL_FEATURES = ["apps", "browser_use", "browser_use_external", "computer_use",
                 "image_generation", "in_app_browser", "hooks", "goals", "code_mode_host"]
MINIMAL_BASE = ('"Eres un motor de decisión. Devuelve solo el JSON pedido. '
                'No ejecutes comandos ni uses herramientas."')
TRIM = {
    "include_permissions_instructions": "false",
    "include_environment_context": "false",
    "include_apps_instructions": "false",
    "web_search": '"disabled"',
    "project_doc_max_bytes": "0",
}

VARIANTS: dict[str, dict] = {
    "baseline": {},
    "no_features": {"disable_features": TOOL_FEATURES},
    "trim": {"overrides": TRIM, "disable_features": TOOL_FEATURES},
    "trim+base": {"overrides": {**TRIM, "base_instructions": MINIMAL_BASE},
                  "disable_features": TOOL_FEATURES},
    "trim+base+low": {"overrides": {**TRIM, "base_instructions": MINIMAL_BASE,
                                    "model_reasoning_effort": '"low"'},
                      "disable_features": TOOL_FEATURES},
}

# Todo lo que añade tools propias de Codex: sin ellas no puede actuar, solo decidir (ADR-0001).
ALL_TOOL_FEATURES = TOOL_FEATURES + [
    "shell_tool", "unified_exec", "unified_exec_tty", "multi_agent", "plugins", "remote_plugin",
    "view_image", "sleep_tool", "skill_search", "tool_suggest", "guardian_approval",
    "realtime_conversation", "workspace_dependencies", "skill_mcp_dependency_install",
    "shell_snapshot", "in_app_local_automation", "tool_call_mcp_elicitation", "mentions_v2",
]
VARIANTS["max_trim"] = {"overrides": TRIM, "disable_features": ALL_TOOL_FEATURES}
VARIANTS["max_trim+plan_off"] = {
    "overrides": {**TRIM, "include_plan_tool": "false", "include_apply_patch_tool": "false",
                  "tools.view_image": "false"},
    "disable_features": ALL_TOOL_FEATURES}

MIN_INSTRUCTIONS = ("Eres un motor de decisión. Devuelve solo el JSON que se te pide. "
                    "No ejecutas comandos ni usas herramientas.")
TRIM2 = {**TRIM, "include_collaboration_mode_instructions": "false"}
VARIANTS["max_trim+instr"] = {"overrides": TRIM2, "disable_features": ALL_TOOL_FEATURES,
                              "instructions": MIN_INSTRUCTIONS}

REQUEST = ModelRequest(
    system="Eres un agente. Decide el siguiente paso.",
    tools=[ToolSpec("shell.exec", "Ejecuta bash", {"type": "object",
                    "properties": {"command": {"type": "string"}}})],
    messages=[Message("user", "Cuenta los ficheros de /tmp.")],
)


async def main(names: list[str]) -> None:
    results = []
    for name in names or list(VARIANTS):
        provider = CodexCliProvider(**VARIANTS[name])
        start = time.monotonic()
        try:
            resp = await provider.complete(REQUEST)
            row = {"variant": name, "ok": True, "input": resp.usage.prompt_tokens,
                   "cached": resp.usage.cached_tokens, "output": resp.usage.completion_tokens,
                   "decision": resp.decision.model_dump(), "violations": resp.violations}
        except Exception as exc:  # noqa: BLE001 — una variante rota no para el bench
            row = {"variant": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        row["seconds"] = round(time.monotonic() - start, 1)
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "decision"}, ensure_ascii=False),
              flush=True)
    out = ROOT / "var" / "bench"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"codex-overhead-{time.strftime('%Y%m%dT%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"-> {path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
