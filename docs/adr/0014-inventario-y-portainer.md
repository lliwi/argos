# ADR-0014 — Inventario de infraestructura y MCP de Portainer (UC-3)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-05, RF-09, RF-19, RF-21, RF-SEC-01/04, RF-OB-11, RF-GOV-04, P2, UC-3

## Contexto
Gestionar infra propia (Portainer, NAS…) necesita credenciales. Pasarlas por el chat las mandaría
al modelo y a la auditoría (como ocurrió con una contraseña el 2026-09-27). El usuario pidió un
fichero donde guardar IPs, claves, usuarios y notas para no repetirlas por el chat.

## Decisión
- **Inventario** `secrets/inventory.yaml` (git-ignored, modo 600; ejemplo versionado en
  `inventory.example.yaml`). Por servicio: campos de documentación (url, endpoint, username,
  notes) y campos **secretos** (api_key, token, password…).
- Los secretos se registran en el redactor (RF-OB-11) y se **inyectan por entorno** a la
  herramienta que los usa; **nunca** se muestran al modelo ni entran en la auditoría (RF-SEC-04).
- `infra.inventory` (tool de lectura) da al agente la **vista pública** (sin secretos, solo indica
  cuáles hay), para que sepa qué infraestructura existe sin contárselo por el chat.
- **MCP de Portainer** (`argos.mcp_servers.portainer`) sobre el API de Portainer (cabecera
  `X-API-Key`) y su proxy Docker. Lectura (listar/inspeccionar/logs/stacks/endpoints) = `read`,
  sin aprobación. Acciones de ciclo de vida (start/stop/restart de contenedores) = `destructive`
  (meta → RF-21): exigen aprobación humana (RF-GOV-04) y respetan dry-run (RF-19). Sin ejecución
  de comandos arbitrarios.
- URL, endpoint y api key salen del inventario (servicio `portainer`); se editan en el fichero, y
  cada nueva tarea relee el inventario (no hace falta recargar).
- **Capacidad potente explícita**: `powerful: true` en el perfil infra. Como sus credenciales ya
  no viven en `secrets:` sino en el inventario, este flag mantiene P2/RF-SEC-03: infra no lee
  contenido no confiable y un webhook no puede dirigirse a él (RF-GOV, scheduler).

## Consecuencias
- El inventario es texto plano protegido por permisos de fichero, no cifrado como el vault SOPS
  (ADR-0005). Es el compromiso pragmático que pidió el usuario; si se instala SOPS/age, puede
  migrarse. Redacción y no-exposición al modelo se mantienen en ambos casos.
- Cambiar de credencial/host = editar `inventory.yaml`; no se expone una tool para reescribirlo
  desde el chat (sería una vía para reapuntar servicios a un host atacante).
