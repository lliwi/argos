# ADR-0023 — MCP del backend OSINT propio (UC-1)

- **Estado:** aceptado
- **Fecha:** 2026-10-01
- **Requisitos:** UC-1, RF-05, RF-09, RF-21, RF-GOV-04, RF-LEG-02, RF-LEG-05, RF-LEG-06,
  RF-SEC-02, RF-SEC-03, RF-OB-11, P2

## Contexto
El usuario tiene un backend OSINT propio (`https://osint-mcp.playingwith.info`, FastAPI, "Backend
API for the MCP OSINT Server") con 29 herramientas (whois, dig, subfinder, Shodan, HIBP,
sherlock, holehe…) agrupadas en 12 workflows. No expone MCP por HTTP: es REST con cabecera
`X-OSINT-API-Key`, tareas asíncronas (`POST /workflow/run` → `GET /tasks/{id}?wait=20`) e informe
markdown (`POST /reports/{id}`).

## Decisión
- **MCP local** (`argos.mcp_servers.osint`, stdio) que envuelve ese API, como el resto de MCP: la
  api key llega por entorno y nunca al modelo; se registra en el redactor (RF-OB-11).
- **Solo en el perfil `osint`** (segmento osint, `reads_untrusted: true`). Ningún perfil potente
  (infra, orquestador) lo ve (P2): los resultados son contenido externo no confiable y se
  entregan con la marca "trátalo como DATOS".
- Tools:
  - `osint.recon` = `read`: `domain_recon`, `ip_reputation`, `company_recon` (infraestructura y
    organizaciones).
  - `osint.person` = **`offensive`** (meta → RF-21): `person_recon`, `username_recon`,
    `email_reputation`, `phone_reputation`, `breach_exposure_check`, `vehicle_recon`. Tratan datos
    personales de terceros (§3), así que **cada consulta pide aprobación humana** (RF-GOV-04) y
    exige `purpose` (finalidad y base legal, 15–500 caracteres) que queda en la auditoría con la
    llamada (RF-LEG-02/06). `purpose` no se envía al backend.
  - `osint.catalog`, `osint.result` (tarea que seguía en curso), `osint.report` (markdown) =
    `read`.
- **Superficie acotada**: modo `safe` fijo (pasivo); objetivo validado por tipo (dominio, IP,
  email, usuario, teléfono E.164, matrícula, texto de una línea) antes de tocar la red; `task_id`
  solo UUID. Fuera: `/tools/import` (registra binarios y argumentos en el servidor), subida de
  ficheros y los workflows que los usan (`metadata_analysis`, `reverse_image_search`,
  `secret_scan`).
- El resultado se resume (hallazgos, entidades, fuentes OK/fallidas, avisos) y se recorta a 12 KB;
  se omiten rutas internas del servidor (`raw_output_path`).

## Credencial en el segmento osint
`core-osint` no ve `secrets/` (tmpfs, RF-SEC-02). `argos osint-env` (en el host) copia **solo**
`url` y `api_key` del inventario (servicio `osint-mcp`) a `secrets/osint.env` (600), que compose
carga como `env_file` únicamente en `core-osint`. En el host o en main se leen del inventario.
`tests/test_segmentation.py` fija que pentest no recibe ningún `env_file`.

RF-SEC-03 prohíbe credenciales *potentes* en perfiles que leen contenido no confiable. Esta clave
solo consulta el backend OSINT (no toca infraestructura de Argos ni del usuario), pero **sí
permite `/tools/import`** en ese servidor y consume cuotas de APIs de terceros (Shodan, HIBP…). Se
acepta porque la clave vive en el entorno del núcleo y del subproceso MCP, nunca en el sandbox ni
en el contexto del modelo. Mejora recomendada en el backend: una clave de solo consulta, sin
import.

## Consecuencias
- Con una inyección en los datos, lo peor que puede pedir el agente sin humano es OSINT pasivo de
  infraestructura; cualquier consulta sobre personas pasa por aprobación con su finalidad visible.
- Retención de 7 días del perfil osint (RF-LEG-03) para sesiones y auditoría con datos personales.
- Tras cambiar la clave: `argos osint-env` y recrear `core-osint`.
