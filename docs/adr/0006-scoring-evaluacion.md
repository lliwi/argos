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

## Actualización 2026-09-30 — verdad en vivo y evaluación programada
- Nueva suite `argos` (eficacia real) junto a `golden` (mecánica del arnés, CI).
- Check `live`: el evaluador obtiene la verdad consultando el servicio real sin modelo
  (`argos.eval.probes`) y la compara con la respuesta final (recuento, nombres, número con
  tolerancia o elección). Mide acierto, no solo "llamó a la tool". Si la sonda falla, el check
  falla con el motivo (sin verdad no se puntúa).
- Los checks evalúan el árbol de sesiones (subagentes incluidos).
- `kind: eval` en el scheduler: corre la suite, compara con la corrida anterior del mismo
  proveedor (`compare_to_baseline`) y publica `eval_report`, que el puente de Matrix envía.
- Límite: los segmentos osint/pentest no pueden evaluarse desde el daemon de main (aislamiento,
  ADR-0008); sus tareas se ejecutan en su núcleo con `docker compose run core-<seg> eval run`.
