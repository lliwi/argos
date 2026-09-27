# Plugin de Herdr para Argos

Abre el chat de Argos (tarea → progreso → aprobaciones en el mismo panel) y la consola de
operador. Si el núcleo persistente no está en marcha, lo arranca (`compose --profile daemon`).

## Instalación

```bash
herdr plugin link ~/Documents/development/argos/integrations/herdr/argos
herdr plugin list --plugin argos
```

En `~/.config/herdr/config.toml`, apunta `prefix+a` a Argos (y, si quieres conservar claw, muévelo
a `prefix+A`):

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "argos.open-chat"
description = "Abrir Argos"

[[keys.command]]
key = "prefix+A"
type = "plugin_action"
command = "claw.open-chat"
description = "Abrir claw (OpenClaw)"
```

Después: `herdr config check` y `herdr server reload-config`.

## Acciones

| Acción | Qué hace |
|---|---|
| `argos.open-chat` | Chat en un split a la derecha |
| `argos.open-popup` | Chat en un popup (80 %) sin tocar la distribución |
| `argos.open-tab` | Chat en una pestaña «argos» (la reutiliza si existe) |
| `argos.open-console` | Consola de operador: todas las sesiones; marca el pane `blocked` con aprobaciones |
| `argos.check` | Arranca el núcleo si hace falta y registra su salud (`herdr plugin log list --plugin argos`) |

En el chat, `Ctrl-C` durante una tarea solo te desconecta: la sesión sigue en el núcleo y se
retoma con `scripts/argos core attach <id>`. `Ctrl-D` cierra el chat.
