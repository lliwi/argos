# ADR-0019 — MCP de Cloudflare (UC-3)

- **Estado:** aceptado
- **Fecha:** 2026-09-29
- **Requisitos:** RF-05, RF-09, RF-19, RF-21, RF-SEC-04, RF-GOV-04, P2, UC-3

## Decisión
- **MCP** (`argos.mcp_servers.cloudflare`) sobre el API v4 de Cloudflare con un **API token**
  (Bearer, nunca la Global API Key). Token en el inventario (`secrets/inventory.yaml`, servicio
  `cloudflare`, campo `api_key` o `token`); se redacta e inyecta, nunca llega al modelo (ADR-0014).
- Lectura = `read`, sin aprobación: `zones`, `dns_list`, `tunnels` (estado y conectores de
  Cloudflare Tunnel), `analytics` (tráfico diario por zona, GraphQL).
- Cambios DNS = `destructive` (meta → RF-21): `dns_create`, `dns_update`, `dns_delete`. Un
  registro DNS publica o retira un servicio en Internet, así que todos piden aprobación humana
  (RF-GOV-04) y respetan dry-run (RF-19). `dns_update`/`dns_delete` leen antes el registro para
  que la aprobación y la respuesta digan exactamente qué cambia.
- **Superficie acotada**: solo tipos A, AAAA, CNAME, TXT, MX; el nombre debe quedar dentro de la
  zona (relativo de una etiqueta, `@` o FQDN de la zona; lo ambiguo se rechaza); ids y contenido
  validados antes de tocar la red. Sin Workers, WAF, ajustes de zona, cuenta ni tokens.
- Los permisos que falten no rompen la herramienta: devuelve `SIN PERMISO` con el permiso exacto
  a añadir al token. Ojo: para recursos de cuenta (túneles) Cloudflare responde 200 con lista
  vacía en vez de 403; `tunnels` lo señala.

## Permisos del token
Mínimo privilegio. Necesarios: `Zone › Zone › Read`, `Zone › DNS › Edit`. Para todas las
herramientas: `Account › Cloudflare Tunnel › Read` y `Zone › Analytics › Read`. Restringir el
token a las zonas propias (y opcionalmente por IP de origen).

## Consecuencias
- Con `DNS › Edit` el agente puede redirigir dominios propios: la contención es aprobación +
  dry-run + auditoría, como en Portainer/Home Assistant.
- Cierra la Fase 4 (MCPs de infraestructura).
