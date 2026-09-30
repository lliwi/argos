# ADR-0021 — MCP de Notion + skill de uso (UC-4)

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Requisitos:** RF-09, RF-19, RF-21, RF-SK-01, RF-SEC-03, RF-SEC-07, RF-GOV-04, P2, UC-4

## Decisión
- **MCP** (`argos.mcp_servers.notion`) sobre el API de Notion (`Notion-Version: 2022-06-28`) con
  el token de una integración interna. Token en el inventario (servicio `notion`, `api_key`);
  se redacta e inyecta, nunca llega al modelo. El alcance real es lo compartido con la
  integración en Notion.
- Herramientas: lectura (`search`, `read_page`, `database_schema`, `query_database`) = `read`;
  escritura reversible (`create_page`, `append`, `update_properties`) = `write`; `archive`
  (borrado a la papelera) = `destructive` → aprobación humana + dry-run.
- **Escritura guiada por el esquema real**: el modelo pasa valores simples por nombre de propiedad
  y el servidor los convierte según el esquema de la base; rechaza propiedades desconocidas, de
  solo lectura, fechas mal formadas y opciones de select/status que no existen (no se crean
  opciones por accidente). El contenido entra como Markdown sencillo convertido a bloques.
- **Perfil `personal`, no `infra`/`orchestrator`**: el workspace contiene material externo
  (newsletters, transcripciones, OSINT) → leerlo es leer contenido no confiable. Por P2 no puede
  convivir con credenciales potentes. Todo resultado va como `<untrusted>`. El orquestador
  delega en `personal`.
- **Skill `notion`** (`skills/notion/SKILL.md`): seguridad (lo leído es dato; nada de secretos
  en Notion; archivar solo a petición explícita), buscar antes de crear, esquema antes de
  escribir, convenciones de la base `TODO` (estados, áreas, cierre con evidencia) y de informes.

## Consecuencias
- Un contenido malicioso en una página podría intentar que el agente escriba en Notion: el daño
  queda acotado al propio workspace (sin credenciales potentes en el perfil, archivado con
  aprobación, todo auditado).
- Las capacidades de la integración (leer/insertar/actualizar) se configuran en Notion; el API no
  permite consultarlas sin escribir. Un 403 se reporta como capacidad faltante.
