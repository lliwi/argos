# ADR-0015 — MCP de Home Assistant (UC-3)

- **Estado:** aceptado
- **Fecha:** 2026-09-28
- **Requisitos:** RF-05, RF-09, RF-19, RF-21, RF-SEC-04, RF-GOV-04, P2, UC-3

## Decisión
- **MCP** (`argos.mcp_servers.homeassistant`) sobre el API REST de Home Assistant (Bearer con
  token de larga duración). Lectura (`list_entities`, `get_state`) = `read`, sin aprobación.
  Acción (`call_service`, p. ej. `light.turn_on`) = `destructive` (meta → RF-21): aprobación
  humana (RF-GOV-04) y dry-run por defecto en el perfil infra (RF-19).
- **Sin plantillas ni comandos**: solo estados y llamadas a servicio. `domain`/`service` deben ser
  identificadores simples y `entity_id` con formato `dominio.objeto`; formatos inválidos se
  rechazan antes de tocar la red.
- URL y token vienen del **inventario** (`secrets/inventory.yaml`, servicio `homeassistant`); el
  token se redacta e inyecta, nunca llega al modelo (ADR-0014). Perfil infra (segmento main,
  `powerful: true`), red local ya alcanzable por CIDR RFC1918.

## Consecuencias
- El token de larga duración de HA da control amplio del hogar: la contención es aprobación +
  dry-run + auditoría, como en Portainer.
- Pendiente Fase 4: Cloudflare.
