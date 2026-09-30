# ADR-0022 — Chat: edición, adjuntos e integración como agente en Herdr

- **Estado:** aceptado
- **Fecha:** 2026-09-30
- **Requisitos:** RF-06, RNF-05, RF-SEC-07, UC-4

## Decisión
- **Entrada del chat con prompt_toolkit** (`argos.channels.chat_input`, solo en el host, import
  diferido): edición con flechas, historial en `var/…/chat_history`, Enter envía y Mayús-Enter
  (o Alt-Enter/Ctrl-J) salta de línea. Para distinguir Mayús-Enter, mientras se escribe se
  activa xterm modifyOtherKeys (`ESC[>4;1m`, lo soporta Herdr) y se reconocen `ESC[27;2;13~` y
  el `ESC[13;2u` de kitty; pegado entre corchetes multilínea sin enviar.
- **Adjuntos**: `/adjuntar`, arrastrar ficheros (se detecta que lo pegado son rutas existentes) y
  Ctrl-V con imagen en el portapapeles (`wl-paste`/`xclip`). La API `POST /sessions` acepta
  `attachments: [{name, data_b64}]` validados en `argos.attachments` (máx. 10, 10 MB/25 MB,
  nombre plano saneado, sin rutas). Se escriben en `in/` del workspace y la tarea recibe una nota
  marcándolos como datos. Las imágenes (máx. 5) se pasan al modelo en cada turno de la sesión
  raíz (`ModelRequest.images` → `codex exec --image`, colocado justo tras `exec`).
- **Herdr**: el chat se anuncia como agente (`pane report-agent`, idle/working/blocked) y fija el
  título de su terminal «argos · chat». `argos.open-chat` abre una pestaña «argos» reutilizable
  con el chat maximizado (`pane zoom --on`), localizando el chat por ese título.

## Consecuencias
- Nueva dependencia `prompt-toolkit` (host). El núcleo no la importa: no requiere reconstruir.
- Enviar imágenes en cada turno cuesta tokens: de ahí el tope de 5.
- Ctrl-C en el prompt borra la línea (como en un shell); salir es Ctrl-D.
