# ADR-0009 — Carga diferida de tools solo con catálogos grandes

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-10, RF-CTX-02, RNF-03, RF-EV-04

## Contexto
Con `loop.lazy_tools_over: 12`, el perfil `personal` (14 tools) activaba la carga diferida: las
tools aparecen sin parámetros y el agente debe llamar a `tools.load` antes de usarlas. En la línea
base de Codex eso supuso 13 pasos extra en 9 tareas: 68,182 de 263,088 tokens (26 %).

Con Codex como motor cada paso cuesta ~5k tokens fijos (ADR-0007), mientras que enviar los
esquemas completos de 14 tools cuesta del orden de 1–2k. Un paso extra sale más caro que lo que
ahorra.

## Experimento (A/B con la suite dorada, un corrida por variante)
`argos eval run golden --provider codex --set loop.lazy_tools_over=40`

| | A (umbral 12) | B (umbral 40) |
|---|---:|---:|
| Tasa de éxito | 9/9 | 9/9 |
| Tokens medios por tarea | 29,232 | 21,082 (−28 %) |
| Pasos medios | 5.0 | 3.44 |
| Puerta de regresión | — | sin regresiones |

## Decisión
Umbral por defecto 40: la carga diferida queda para catálogos grandes (MCP de dominio, Fase 4).
La corrida B pasa a ser la línea base (`evals/baselines/golden-codex.json`).

## Consecuencias
- Revisar el umbral cuando se añadan MCP con muchas tools, midiendo de nuevo con A/B.
- Una sola corrida por variante: la diferencia es grande y consistente en las 9 tareas, pero
  conviene confirmar con `--repeat 3` en la próxima revisión del motor.
