# ADR-0017 — MCP de descargas (Jackett + Transmission) (UC-3)

- **Estado:** aceptado
- **Fecha:** 2026-09-28
- **Requisitos:** RF-05, RF-09, RF-19, RF-21, RF-SEC-04, RF-GOV-04, P2, UC-3

## Decisión
- **MCP `media`** que une Jackett (búsqueda en indexadores) y Transmission (cliente de descargas)
  del propio homelab. `media.search` y `media.downloads` = `read` (sin aprobación); `media.add` y
  `media.control` (start/stop/remove) = `destructive` (meta → RF-21): aprobación humana
  (RF-GOV-04) y dry-run (RF-19).
- **Sin comandos ni rutas locales**: `media.add` solo acepta enlaces `magnet:` o URLs http(s) a un
  `.torrent`; `media.control` solo start/stop/remove; formatos inválidos se rechazan. Jackett usa
  su API v2.0 (apikey por query); Transmission su RPC con el handshake 409 → session id.
- URLs y api key vienen del **inventario** (servicios `jackett`, `transmission`); la api key se
  redacta e inyecta, nunca al modelo (ADR-0014). Perfil infra (segmento main), red local por CIDR.
- `remove` no borra los ficheros ya descargados (no se expone `delete-local-data`).

## Consecuencias
- Es infraestructura propia del usuario: Argos conecta con sus servicios y gestiona sus descargas;
  qué se busca/descarga es responsabilidad del usuario, igual que con Portainer o Home Assistant.
- Pendiente Fase 4: Cloudflare.
