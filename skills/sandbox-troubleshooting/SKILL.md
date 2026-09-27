---
name: sandbox-troubleshooting
description: Diagnosticar fallos en el sandbox (instalaciones, red bloqueada, permisos, timeouts).
version: 1.0.0
---
- `exit_code=124` o "(timeout)": el comando superó el límite. Divide el trabajo o usa `timeout_s`.
- "CONNECT tunnel failed, response 403" o "argos-egress": el dominio no está en la allowlist. No
  reintentes ni busques rodeos (mirrors, IPs): explica qué dominio hace falta y termina.
- "Read-only file system": solo `/workspace/out`, `/tmp` y `$HOME` son escribibles; `/workspace/in`
  es de solo lectura por diseño.
- `pip install` falla por compilación: prueba una versión con wheel binario (`pip install
  "paquete<X"`) o `--only-binary=:all:`.
- No uses `sudo` ni `apt`: el sandbox no tiene privilegios.
