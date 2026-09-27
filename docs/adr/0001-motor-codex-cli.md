# ADR-0001 — Motor de modelo: Codex CLI como paso de completado (D-1)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-01, RNF-02, RNF-03, RF-OB-02

## Contexto
Se quiere aprovechar la suscripción de ChatGPT (RNF-02). `codex exec` es en sí un agente con su
propio bucle y su propio shell, pero el objetivo del proyecto es **construir** el bucle (RF-01),
la auditoría y la gobernanza.

## Decisión
- Interfaz `ModelProvider` (`src/argos/model/base.py`) con un único método `complete()` que devuelve
  **una decisión**: `tool_call` o `final`.
- `CodexCliProvider` invoca `codex exec --json --ephemeral --ignore-user-config -s read-only` en un
  directorio vacío, con `--output-schema` que fuerza la forma de la decisión. Codex actúa como motor
  de completado; **el bucle, las tools y el sandbox son de Argos**.
- Los tokens se leen de `turn.completed.usage`. Coste USD = 0 (suscripción); se registra un coste
  equivalente configurable (`model.cost_per_mtok`) para presupuestos.
- Si Codex emite ítems `command_execution`/`file_change` (intentó actuar por su cuenta) se audita
  como `validation_error`: es una violación del contrato.
- `FakeProvider` guionizado para tests y evaluación sin red.

## Consecuencias
- Cada paso relanza un proceso y reenvía todo el contexto: sin control fino de *prompt caching*
  (RF-CTX-01) ni de parámetros de muestreo. Aceptable en Fase 1; un `OpenAIProvider` llegará detrás
  de la misma interfaz.
- El formato `--json` de Codex no es un contrato estable: el parser es tolerante y está cubierto por
  fixtures en `tests/fixtures/`.
