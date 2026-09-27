# ADR-0008 — Segmentación de seguridad por contenedor local

- **Estado:** aceptado (parcial: ver "Pendiente")
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

## Pendiente
- Los núcleos siguen montando el socket Docker para crear sandboxes. Eso da control del host a
  cualquier núcleo comprometido, así que el aislamiento actual es de red, datos y secretos, **no**
  de host. Falta mover la creación de sandboxes a un componente separado con API restringida.
- Cifrado en reposo de la auditoría viva (hoy solo los backups van cifrados).
- Aislamiento multi-máquina vía Herdr/SSH (Fase 3).
