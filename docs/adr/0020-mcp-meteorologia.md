# ADR-0020 — MCP de meteorología (UC-4)

- **Estado:** aceptado
- **Fecha:** 2026-09-29
- **Requisitos:** RF-09, RF-21, UC-4

## Decisión
- **MCP** (`argos.mcp_servers.weather`) con una herramienta de solo lectura, `weather.hourly(city,
  hours)`, que extrae de la vista por horas de eltiempo.es (`/<ciudad>.html?v=por_hora`, ~72 h):
  temperatura, lluvia (mm y probabilidad) y viento (km/h y dirección), más un resumen por día
  (mín/máx, lluvia total, horas con lluvia, viento máximo) útil para tareas programadas.
- **Solo números al modelo**: el parser (stdlib `html.parser`, sin dependencias nuevas) lee
  atributos y nodos concretos (`ul#meteograma`, `data-temperature`, `data-speed`…); ningún texto
  libre de la página llega al modelo, así que no abre vía de inyección. `city` es un slug validado
  (`[a-z0-9-]`); ciudad por defecto `barcelona` (`ARGOS_WEATHER_CITY`).
- Si el HTML cambia y no se reconoce nada, devuelve error explícito, nunca datos parciales.
- Perfiles: `personal` y también `orchestrator` (lectura sin credenciales: responde sin delegar).

## Consecuencias
- Depende del marcado de eltiempo.es (scraping): un rediseño lo rompe de forma visible. Alternativa
  estable si ocurre: API de Open-Meteo (JSON, sin clave).
- Al añadir un segundo MCP a `personal` apareció un cuelgue al cancelar/apagar sesiones con varios
  MCP: el `task.cancel()` nativo caía dentro de la salida de `stdio_client`. Corregido: cada
  conexión MCP vive en su propia tarea propietaria (`McpConnections`) y `serve()` espera a las
  sesiones al salir aunque se cancele.
