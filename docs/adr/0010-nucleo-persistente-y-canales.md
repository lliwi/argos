# ADR-0010 — Núcleo persistente, scheduler y consola con Herdr

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-04, RF-06, RF-08, RF-13..15, RF-20, RF-GOV-05, RNF-05, RNF-11, P1, P2

## Decisión
- `argos serve` es un proceso de larga duración (servicio compose `daemon`) con API HTTP sobre un
  socket Unix `var/segments/<seg>/run/argos.sock`. El directorio es 700 y el socket 600: la
  autenticación es el permiso del fichero.
- Los canales son clientes de esa API y retransmiten **los mismos eventos de auditoría** (SSE),
  ya redactados. No hay un segundo modelo de eventos para canales (P1, P4).
- Aprobaciones: el núcleo publica `approval_request` en el flujo; cualquier canal puede responder
  (RF-20) y la auditoría registra quién y por dónde. Sin respuesta en `approval.timeout_s`, la
  acción no se ejecuta (RF-GOV-05).
- Scheduler en proceso (cron propio, sin dependencia): cada ejecución es una sesión propia con
  canal `scheduler`, sin aprobador humano y sin solapes. Webhooks en TCP (localhost) con token
  por hook; el payload entra como `<untrusted>` y un hook no puede apuntar a un perfil con
  secretos potentes (P2).
- `argos console` (canal TUI) corre en el host. Dentro de un pane de Herdr informa del estado vía
  `herdr pane report-agent` (working / blocked / idle) y notifica aprobaciones. El socket de
  Herdr **no** se monta en el contenedor del núcleo: controla terminales del usuario (P2).
  `scripts/herdr-argos.sh` monta la disposición (consola + logs) desde un pane de Herdr.

## Consecuencias
- Herdr hospeda procesos ordinarios, no un "agente" nativo (no admite tipos propios): la
  integración es por informe de estado y notificaciones, suficiente para operar Argos.
- La cancelación de una sesión se propaga al paso en curso (bug encontrado por los tests: la tool
  seguía ejecutándose tras cancelar).
- Pendiente: canal Matrix (siguiente bloque) sobre esta misma API.
