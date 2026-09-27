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
scripts/herdr-argos.sh                           # desde un pane de Herdr: consola + logs
```
Tareas programadas y webhooks en [config/schedules.yaml](config/schedules.yaml)
([ADR-0010](docs/adr/0010-nucleo-persistente-y-canales.md)).

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
