# ADR-0018 — MCP del NAS (SMB + SNMP)

- **Estado:** aceptado
- **Fecha:** 2026-09-29
- **Requisitos:** RF-05, RF-09, RF-19, RF-21, RF-SEC-04, RF-GOV-04, P2, UC-3

## Contexto
Gestionar el NAS (TerraMaster): manejar ficheros (listar, leer, mover) y ver el almacenamiento
(volúmenes, uso). El NAS ofrece SMB (445), SNMP (161) y SSH (9222).

## Decisión
- **Sin SSH / sin ejecución de comandos.** Aunque el NAS expone SSH en 9222, no se usa: un
  `ssh exec` libre sería una superficie de RCE (ya rechazada para Kali). Se usan protocolos que
  mapean a operaciones concretas:
  - **Ficheros por SMB** (`smbprotocol`): `nas.list`, `nas.read` (texto, ≤200 KB) = `read`;
    `nas.mkdir` = `write`; `nas.move`, `nas.delete` = `destructive` (meta → RF-21): aprobación
    humana (RF-GOV-04) y dry-run (RF-19). `delete` no borra carpetas con contenido; `move` exige
    mismo recurso compartido.
  - **Almacenamiento por SNMP** (`puresnmp`, v2c): `nas.storage` recorre `hrStorageTable` y
    muestra los sistemas de ficheros (memoria/swap se filtran para no confundir con "disco lleno").
- **Rutas validadas**: cada segmento debe ser un nombre de fichero válido; se rechazan `..`,
  rutas absolutas o nulos (anti-traversal). El modelo referencia `recurso/carpeta`, nunca UNC ni
  rutas del host.
- **Credenciales del inventario** (`nas`: url, user, password, community); `password` y
  `community` son secretos → redactados e inyectados por entorno, nunca al modelo (ADR-0014). La
  `community` SNMP se añadió a `SECRET_FIELDS`.

## Consecuencias
- Nuevas dependencias: `smbprotocol` y `puresnmp` (ambas Python puro); el núcleo se reconstruye.
- El NAS es del usuario (UC-3): Argos opera sobre sus ficheros con aprobación en las acciones.
- Pendiente Fase 4: Cloudflare.
