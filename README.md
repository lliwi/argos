# Argos

Arnés de agentes personal, autoalojado, **auditable y evaluable**. Requisitos en
[REQUISITOS-TECNICOS.md](REQUISITOS-TECNICOS.md); decisiones de arquitectura en [docs/adr/](docs/adr/).

## Estado
Fase 1 (bucle mínimo): bucle propio, sandbox Docker por sesión con egress por allowlist,
auditoría JSONL+SQLite, presupuestos/kill switch/HITL y runner de evaluación.

## Arranque rápido
```bash
uv sync
docker compose build            # imagen de sandbox + proxy de egress
docker compose up -d egress-main   # sandbox del segmento main (host: ARGOS_SEGMENT=main)
uv run argos run --profile personal "instala pandas y calcula la media de 1..10"
uv run argos audit list
uv run argos audit replay <session_id>
uv run argos eval run golden    # tareas doradas (FakeProvider por defecto)
```

## Ejecución en contenedor (segmentada)
Cada segmento de seguridad (`main`, `osint`, `pentest`) tiene núcleo, red de sandbox, proxy de
egress y datos propios ([ADR-0008](docs/adr/0008-segmentacion-por-contenedor.md)).
```bash
scripts/setup-env.sh                                   # genera .env (UID, grupo docker, rutas)
docker compose --profile build --profile core build    # imágenes sandbox, proxy y núcleo
docker compose up -d egress-main egress-osint egress-pentest
docker compose run --rm --entrypoint codex core login --device-auth   # una vez
scripts/argos run "..."                                # segmento main
ARGOS_SEGMENT=osint scripts/argos run -p osint "..."   # segmento osint
scripts/argos purge --dry-run                          # retención (RF-LEG-03)
scripts/argos backup                                   # backup cifrado con age
```
Las credenciales de Codex del contenedor viven en `~/.config/argos/codex-home` (fuera del repo).

Motor de modelo: `codex` (Codex CLI con suscripción ChatGPT; requiere `codex login`) o `fake`
(respuestas guionizadas, para tests y CI). Ver [ADR-0001](docs/adr/0001-motor-codex-cli.md).

## Núcleo persistente y consola (Fase 3)
```bash
docker compose --profile daemon up -d daemon     # núcleo 24/7: API, scheduler, webhooks
scripts/argos core status                        # salud, sesiones, aprobaciones, tareas
scripts/argos core submit "…"                    # envía y sigue una tarea (o --detach)
scripts/argos core approve <id> [--deny]         # responde una aprobación desde cualquier sitio
uv run argos console                             # consola de operador (en el host)
uv run argos chat                                # conversación con hilo y memoria (/help)
uv run argos memory list | add | edit | pin | forget   # lo que Argos recuerda (RF-18)
scripts/herdr-argos.sh                           # desde un pane de Herdr: consola + logs
```
Tareas programadas y webhooks en [config/schedules.yaml](config/schedules.yaml)
([ADR-0010](docs/adr/0010-nucleo-persistente-y-canales.md)).

## Matrix (Fase 3)
```bash
scripts/matrix-login.sh https://matrix.tudominio.org argos   # token del bot → .env.matrix
# config/argos.yaml → matrix.homeserver, matrix.user_id, matrix.allowed_users
docker compose --profile daemon up -d daemon matrix
```
Invita al bot a una sala **sin cifrar** y escríbele. Aprobaciones con «sí»/«no» o ✅/❌; `!ayuda`
([ADR-0012](docs/adr/0012-canal-matrix.md)).

## Pentest de servicios propios (UC-2)
Auditoría de tus propios servicios web con Kali, con alcance y autorización obligatorios
([ADR-0013](docs/adr/0013-mcp-kali-pentest.md)). En `config/profiles/pentest.yaml`:
```yaml
scope: ["app.midominio.org", "*.lab.midominio.org", "10.0.0.0/24"]
authorization_ref: "autorizacion-propia-2026-01"   # constancia de que es tuyo/autorizado
dry_run: true    # simula por defecto; ponlo a false y aprueba cada acción para ejecutar
```
```bash
docker compose --profile kali build kali
docker compose --profile core --profile kali up -d core-pentest kali egress-pentest broker
ARGOS_SEGMENT=pentest scripts/argos core submit -p pentest "reconoce app.midominio.org"
```
Sin `scope`+`authorization_ref` no se ejecuta ninguna acción ofensiva; los objetivos fuera de
alcance se rechazan; cada acción real pide aprobación (RF-GOV-04). Skill de apoyo: `web-recon`.

El propio sistema puede instalar herramientas en el contenedor Kali con `kali.install` (nombres de
paquete apt validados) y refrescar índices con `kali.apt_update`; no hay ejecución de comandos
libres. La red de Kali es bridge para poder auditar objetivos reales.

## Infraestructura propia: inventario y Portainer (UC-3)
Guarda IPs, claves, usuarios y notas en `secrets/inventory.yaml` (git-ignored, no van por el chat):
```bash
# opción A: editar el fichero a mano (cp del ejemplo, chmod 600)
# opción B (recomendada para la clave): prompt oculto que no pasa por el chat ni la auditoría:
argos inventory set portainer url http://192.168.0.20:9000
argos inventory set portainer endpoint 1
argos inventory set portainer api_key            # pide la clave de forma oculta
```
Los secretos se inyectan a las herramientas y nunca llegan al modelo ni a la auditoría; el agente
ve solo la documentación con `infra.inventory`. Portainer (perfil infra, segmento main):
```bash
ARGOS_SEGMENT=main scripts/argos core submit -p infra "lista mis contenedores y dime si alguno está parado"
```
Listar/inspeccionar/logs no piden aprobación; parar/arrancar/reiniciar sí (y se simulan con
`dry_run: true` por defecto). Ver [ADR-0014](docs/adr/0014-inventario-y-portainer.md).

**Home Assistant** (mismo patrón, [ADR-0015](docs/adr/0015-mcp-home-assistant.md)): añade el
servicio `homeassistant` al inventario (url + token de larga duración) y usa `homeassistant.*`
(listar entidades y estados sin aprobación; `call_service` para encender/apagar con aprobación).

**Cloudflare** ([ADR-0019](docs/adr/0019-mcp-cloudflare.md)): servicio `cloudflare` con un API
token (`argos inventory set cloudflare api_key`). `cloudflare.*`: zonas, registros DNS, túneles y
analítica sin aprobación; crear/modificar/borrar registros DNS con aprobación. Permisos del token:
`Zone:Read` + `DNS:Edit` (y `Cloudflare Tunnel:Read` + `Analytics:Read` para túneles y tráfico).

**Meteorología** ([ADR-0020](docs/adr/0020-mcp-meteorologia.md)): `weather.hourly` (perfiles
`personal` y `orchestrator`, sin credenciales) da la previsión por horas de eltiempo.es
—temperatura, lluvia y viento— con resumen diario. Ciudad = slug de la URL (`barcelona` por
defecto).

**Notion** ([ADR-0021](docs/adr/0021-mcp-notion.md)): servicio `notion` (token de integración
interna) y `notion.*` en el perfil `personal`: buscar, leer páginas, consultar bases de datos,
crear/añadir/actualizar (validado contra el esquema real) y archivar con aprobación. La skill
`notion` recoge las buenas prácticas (base TODO, informes, seguridad). Comparte en Notion las
páginas que quieras que vea la integración.

## Acceso de red del sandbox
Por defecto el sandbox solo sale a repositorios de paquetes (RF-EX-04). Los perfiles `personal` e
`infra` incluyen además las redes locales (RFC1918: `192.168.0.0/16`, `10.0.0.0/8`,
`172.16.0.0/12`) en su `egress_extra`, para llegar a servicios de casa por IP. Para un host
externo concreto, añade su dominio al `egress_extra` del perfil; no hay un modo "abrir todo"
(sería quitar la defensa frente a exfiltración). Las entradas admiten dominio, `*.dominio`, IP o
rango CIDR. Los perfiles que leen contenido no confiable (osint, pentest) mantienen el egress
restringido.

## Comandos
| Comando | Qué hace |
|---|---|
| `argos run` | Ejecuta una tarea en una sesión nueva |
| `argos audit list/show/replay/cost/diff/metrics` | Revisión de auditoría (RF-OB-04..06) |
| `argos eval run/compare` | Tareas doradas, A/B y puerta de regresión ([evals/README.md](evals/README.md)) |
| `argos kill` / `argos rearm` | Kill switch global (RF-GOV-02) |
| `argos tools` | Catálogo de tools (RF-11) |
| `argos skills list/show/install` | Skills instaladas (§9) |
| `argos purge` / `argos backup` | Retención y copias cifradas |
