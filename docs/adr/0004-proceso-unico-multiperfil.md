# ADR-0004 — Proceso único multi-perfil en Fase 1 (D-4)

- **Estado:** aceptado (revisar en Fase 2)
- **Fecha:** 2026-09-27
- **Requisitos:** RF-05, RF-SEC-02, RF-SEC-03

## Decisión
Los perfiles son configuración (`config/profiles/*.yaml`): tools, secretos, políticas, allowlist
extra, `dry_run`. En Fase 1 un solo proceso los carga. La segmentación real (proceso/máquina por
perfil sensible bajo Herdr por SSH) se aborda en Fase 2; el loader ya **rechaza** perfiles que
combinen `reads_untrusted: true` con secretos marcados `powerful` (P2, RF-SEC-03).
