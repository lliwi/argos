# ADR-0003 — Auditoría JSONL + SQLite, compatible con OTel (D-3)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-OB-01..13, P4, P8

## Decisión
- Cada evento es un modelo pydantic (`src/argos/audit/events.py`) con `trace_id` (=sesión raíz) y
  `span_id`/`parent_span_id`, para poder exportar a OpenTelemetry sin cambiar el modelo.
- Doble escritura: `var/audit/<session>.jsonl` (append-only, legible) + `var/argos.db` (consulta).
- Salidas grandes van a `var/blobs/<sha256>` y el evento guarda la referencia (RF-OB-08).
- Toda escritura pasa por el redactor (RF-OB-11) antes de tocar disco.
