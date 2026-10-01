# ADR-0024 — Concurrencia por perfil y global (RF-GOV-03)

- **Estado:** aceptado
- **Fecha:** 2026-10-01
- **Requisitos:** RF-GOV-03, RF-SEC-02, RF-OB-12

## Contexto
Había un solo semáforo global (`concurrency.max_sessions: 4`) que rechazaba al instante y no
contaba subagentes. Con el orquestador como punto de entrada único (ADR-0016), dos conversaciones
podían delegar a la vez en `infra` y actuar en paralelo sobre Portainer, Home Assistant o el NAS.

## Decisión
- **Límite por perfil** `max_concurrent` en `config/profiles/*.yaml`: `infra: 1` y `pentest: 1`
  (actúan sobre sistemas reales), `personal: 2`, `osint: 2`; sin valor = solo el global.
- **Global** (`concurrency.max_sessions`): solo sesiones raíz, como antes.
- **Los subagentes cuentan** para el perfil en que corren: una delegación a `infra` ocupa el hueco
  de infra. Excepción: un subagente del **mismo perfil** que su padre trabaja dentro del hueco de
  este (contarlo bloquearía al padre contra sí mismo con `max_concurrent: 1`).
- **Cola con espera**: sin hueco, la sesión espera hasta `concurrency.wait_s` (60 s) y luego se
  rechaza (`SessionRefused`) con el límite concreto. Una tarea programada o un mensaje de Matrix ya
  no falla solo porque otra está en marcha. Si el rechazado es un subagente, `agent.delegate`
  devuelve el motivo y el padre sigue.
- Los rechazos se anotan en `var/segments/<seg>/concurrency.jsonl` (metadatos, sin contenido),
  como `purge.jsonl` y `scheduler.jsonl`; no se crean eventos de auditoría huérfanos de sesiones
  que nunca empezaron.

## Límite conocido
Los huecos viven en el proceso. Un `argos run` de la CLI no ve las sesiones del daemon (ni
viceversa). Contarlas por la BD (`sessions.status = running`) dejaría huecos bloqueados por
sesiones que murieron sin cerrarse; si hace falta, se hará con un lease con caducidad.
