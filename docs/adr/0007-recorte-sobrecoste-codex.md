# ADR-0007 — Recorte del sobrecoste fijo de Codex CLI

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RNF-03, RF-CTX-01, RF-CTX-05, ADR-0001

## Contexto
La primera sesión real mostró ~15k tokens de entrada por paso con un contexto propio de ~800
tokens: el ~94% era el prompt base de Codex CLI y las definiciones de sus tools internas. Con
Codex como único motor (decisión de Fase 2), ese sobrecoste domina y anula las optimizaciones de
contexto del núcleo.

## Experimento (reproducible: `scripts/argos-py scripts/bench_codex_overhead.py`)
Mismo prompt mínimo, una llamada por variante, Codex CLI 0.157.1:

| Variante | Tokens entrada | Decisión |
|---|---:|---|
| baseline | 14,307 | correcta |
| desactivar features con tools (apps, browser, computer_use, …) | 11,998 | correcta |
| + `include_*_instructions=false`, `web_search=disabled`, `project_doc_max_bytes=0` | 9,350 | correcta |
| + `base_instructions` / `model_reasoning_effort=low` | 9,350 | sin efecto |
| + desactivar shell/exec/multi_agent/plugins/… | 8,477 | correcta |
| + `model_instructions_file` (prompt base propio de 2 líneas) | **4,177** | correcta |

Sesión real A/B (misma tarea, `argos audit diff`): 45,310 → 14,991 tokens (−67%), misma
secuencia de acciones y resultado; aparece caché de prompt (2,688 tokens en el turno 3).

## Decisión
- Configuración recortada por defecto en `config/argos.yaml` (`model.codex`), con el prompt del
  motor versionado en `prompts/codex-engine.md` e incluido en `prompt_version` (P8).
- Al desactivar `shell_tool`/`unified_exec`, Codex **no puede** ejecutar comandos: el contrato de
  ADR-0001 pasa de "no debe" a "no puede". Se mantiene la detección de violaciones.

## Consecuencias
- Riesgo: sustituir el prompt base puede degradar la calidad de decisión en tareas complejas. Se
  vigila con la suite de evaluación en modo `codex` y su línea base (RF-EV-05).
- Depende de claves de configuración internas de Codex (no contrato estable): el bench se
  re-ejecuta al actualizar Codex CLI.
