# ADR-0012 — Canal Matrix

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-07, RF-08, RF-20, RF-GOV-02, P1, P2

## Decisión
- **Cliente propio sobre httpx** contra la API cliente-servidor v3 (sync, enviar/editar, hilos
  `m.thread`, reacciones), en lugar de `matrix-nio`: sin dependencias nuevas y cada llamada es
  explícita. Consecuencia: **sin E2E**; el bot funciona en salas no cifradas.
- **Cliente más de la API del núcleo** (ADR-0010): mismos eventos, mismas aprobaciones, mismos
  hilos y memoria (ADR-0011). Un hilo de Matrix = una conversación de Argos.
- **Autorización**: solo obedece a `matrix.allowed_users` y solo acepta invitaciones suyas; el
  resto se ignora (y queda en el log). Cualquiera puede invitar a un bot de Matrix.
- **Primer arranque sin replay**: se fija el punto de sincronización sin ejecutar mensajes
  antiguos (evita ejecutar órdenes viejas al reiniciar con estado perdido).
- **UX**: progreso en un único mensaje editado (sin inundar la sala); respuesta final en un
  mensaje nuevo, que es lo que notifica en el móvil.
- **Aprobaciones** (RF-20): «sí»/«no» en el hilo o reacción ✅/❌; auditadas con el usuario de
  Matrix como aprobador. Las de otros canales van a `notify_room` si está configurada.
- **Comandos**: `!estado`, `!memoria`, `!kill <motivo>` (RF-GOV-02 desde el móvil), `!rearm`.
- **Despliegue**: servicio `matrix` sin socket Docker ni clave age; token en `.env.matrix` (600,
  fuera de git), obtenido con `scripts/matrix-login.sh` sin guardar la contraseña.

## Consecuencias
- Si la sala es cifrada, el bot no verá los mensajes: usar una sala sin cifrar dedicada.
- E2E requiere libolm/vodozemac y gestión de dispositivos y claves; se aborda si hace falta.
