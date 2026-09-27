# ADR-0008 — Segmentación de seguridad por contenedor local

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-SEC-01..04, RF-SEC-08, RF-EX-08, RNF-06, RF-LEG-03/04, RNF-10

## Decisión
- Tres segmentos en `config/argos.yaml`: `main` (personal, infra), `osint`, `pentest`. Un perfil
  pertenece a un único segmento y el núcleo rechaza perfiles ajenos (`ARGOS_SEGMENT`).
- Por segmento: servicio de núcleo propio (`core`, `core-osint`, `core-pentest`), red de sandbox
  interna (`argos_sandbox_<seg>`), proxy de egress propio (`egress-<seg>`) y datos propios
  (`var/segments/<seg>`: auditoría, workspaces, blobs; `var/egress/<seg>`: política y eventos).
- Cada núcleo monta el proyecto en solo lectura y solo puede escribir en su segmento. Solo `main`
  recibe la clave age (secretos); osint/pentest no tienen credenciales de servicios (P2).
- Los secretos conocidos se redactan de toda salida de tool **antes** de entrar al contexto del
  modelo (RNF-06), además de en la auditoría. Los datos personales solo se redactan en auditoría.
- Retención: `argos purge` aplica `retention_days` del perfil (osint: 7) o el global; recoge
  blobs huérfanos y anota lo purgado. `argos backup` genera un snapshot cifrado con age.

## Broker de sandbox (completado 2026-09-27)
- Solo el servicio `broker` monta el socket Docker, y no tiene red (`network_mode: none`). Los
  núcleos no tienen el socket ni el grupo docker: piden el sandbox de **su** sesión por
  `var/broker/<seg>/broker.sock` (directorio 700, socket 600), y cada núcleo solo monta el suyo.
- El segmento lo determina el socket por el que llega la petición: un núcleo comprometido no
  puede crear ni usar sandboxes de otro segmento.
- El broker valida cada petición (`sandbox/broker_policy.py`): id de sesión, dominios extra,
  variables de entorno (las de proxy son reservadas), comando y timeout. Imagen, red, proxy,
  límites y workspace montado salen de su configuración, nunca de la petición.
- La política de egress la escribe solo el broker; los núcleos ya no montan `var/egress`.
- Registro propio en `var/broker/broker.jsonl` (nombres de variables, nunca sus valores).
- `sandbox.backend: docker` queda para desarrollo en el host.

## Pendiente
- Cifrado en reposo de la auditoría viva (hoy solo los backups van cifrados).
- Aislamiento multi-máquina vía Herdr/SSH (`herdr --remote`) para perfiles sensibles.
