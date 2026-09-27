# ADR-0011 — Conversaciones encadenadas y memoria con procedencia

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-16, RF-17, RF-18, P2, P3, P8, RF-CTX-05, RF-LEG-03

## Contexto
Cada mensaje del chat era una sesión aislada: el agente no sabía de qué se había hablado ni
recordaba nada entre días. Reenviar el transcript completo resolvería lo primero pero dispara
tokens y degrada el contexto (§14).

## Decisión
- **Estado separado de la auditoría** (`var/segments/<seg>/state.db`, P3): hilos, intercambios y
  memoria. La auditoría dice qué pasó; el estado, qué sabe el agente. Uno por segmento.
- **Hilos**: cada mensaje sigue siendo una sesión propia (auditoría y presupuesto propios). La
  nueva sesión recibe los últimos N intercambios literales y un **resumen incremental** de los
  anteriores, generado con la ruta `internal` solo cuando un intercambio sale de la ventana y
  auditado como turno interno. Nunca el transcript completo.
- **Memoria con procedencia**: `user` (añadida o editada por ti: entra como preferencia fiable) y
  `agent` (guardada con `memory.save`: entra como `<untrusted>`). Defensa contra el
  *memory poisoning*: una inyección que convenza al agente de "recordar" una orden no se
  convierte en instrucción permanente (P2). Editar una memoria del agente la hace tuya.
- **Recuperación selectiva** (RF-17): fijadas + las más relevantes para la tarea con FTS5/BM25 de
  SQLite (sin embeddings ni dependencias). Qué se inyectó queda en `memory_event` (P8).
- Los secretos conocidos se redactan antes de guardar. Los subagentes no reciben memoria ni hilo
  (su contexto limpio es el objetivo, RF-02).
- **Retención**: la memoria del agente solo caduca en perfiles con `retention_days` explícito
  (osint: 7 días, datos de terceros). La tuya, solo si le pones fecha.

## Consecuencias
- Coste por mensaje en hilo: el de la sesión más, a veces, un resumen barato (~5k tokens con
  Codex por su sobrecoste fijo, ADR-0007).
- La relevancia léxica falla con sinónimos ("servidor de ficheros" ≠ "NAS"); `memory.search`
  permite al agente buscar con otras palabras. Embeddings, si hacen falta, en una fase posterior
  medidos con la suite de evaluación.
