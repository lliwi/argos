# Plugin de Herdr para Argos

Abre el chat de Argos (tarea → progreso → aprobaciones en el mismo panel) y la consola de
operador. Si el núcleo persistente no está en marcha, lo arranca (`compose --profile daemon`).

**Núcleo en el servidor de producción** (ADR-0025): si existe `secrets/api-client.env` en el repo,
el plugin se conecta al núcleo remoto (`https://192.168.0.34:8788`, token + certificado fijado) y
no arranca nada en local. Copia del servidor `secrets/api-client.env` y `secrets/api-cert.pem`
(se generan allí con `argos api-setup 192.168.0.34`). Para volver al núcleo local, borra
`secrets/api-client.env`. `argos.check` muestra a cuál está conectado.

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
| `argos.open-chat` | **Principal** (`prefix+a`): pestaña «argos» con el chat maximizado. Si la pestaña existe la reutiliza (y si el chat se cerró, abre otro dentro); si no, la crea |
| `argos.open-split` | Chat en un split a la derecha |
| `argos.open-popup` | Chat en un popup (80 %) sin tocar la distribución |
| `argos.open-tab` | Chat en una pestaña «argos» (igual que open-chat) |
| `argos.open-console` | Consola de operador: todas las sesiones; marca el pane `blocked` con aprobaciones |
| `argos.check` | Arranca el núcleo si hace falta y registra su salud (`herdr plugin log list --plugin argos`) |

El chat se anuncia en Herdr como agente `argos` (aparece en la lista **agentes** de la barra
lateral): `idle` esperando, `working` con una tarea, `blocked` cuando pide una aprobación.

## Uso del chat

| Tecla / comando | Qué hace |
|---|---|
| ←/→, Ctrl-A/E, Ctrl-W… | Moverse y editar el texto |
| ↑/↓ | Líneas del mensaje y, en los extremos, historial (se guarda entre sesiones) |
| Enter · Mayús-Enter (o Alt-Enter, Ctrl-J) | Enviar · salto de línea |
| Pegar (Ctrl-Shift-V del terminal) | Inserta el texto entero, multilínea, sin enviarlo |
| Arrastrar ficheros al terminal | Se adjuntan (en vez de pegar su ruta) |
| Ctrl-V | Pega la imagen del portapapeles como adjunto (o su texto si no hay imagen) |
| `/adjuntar <ruta…>` | Adjunta ficheros (Tab autocompleta rutas) |
| `/adjuntos` · `/quitar [n]` | Ver · descartar adjuntos pendientes |
| Ctrl-C | Con una tarea en curso: desconecta (la sesión sigue en el núcleo, `scripts/argos core attach <id>`). En el prompt: borra la línea |
| Ctrl-D | Cerrar el chat |

Los adjuntos (máx. 10, 10 MB cada uno, 25 MB en total) viajan con el siguiente mensaje, se
guardan en `in/` del workspace de la sesión y las imágenes se muestran al modelo.
