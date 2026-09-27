# ADR-0006 — Puntuación de tareas doradas: determinista primero (D-7)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-EV-01..07, CA-8

## Decisión
- Tarea dorada = YAML en `evals/golden/` con `prompt`, `profile`, `provider`, guion opcional para
  `fake` y lista de `checks` deterministas sobre workspace y auditoría.
- `score` = fracción de checks superados; `passed` = todos los checks obligatorios superados.
- Un tipo de check `llm_judge` queda reservado (no implementado) para tareas abiertas.
- Cada corrida emite `eval_run` con las versiones capturadas de la sesión (RF-EV-06).
