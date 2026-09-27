# ADR-0002 — Sandbox Docker persistente por sesión (D-2)

- **Estado:** aceptado
- **Fecha:** 2026-09-27
- **Requisitos:** RF-EX-01..09, CA-2, CA-6

## Decisión
- Un contenedor por sesión (`argos-sbx-<session>`), creado al primer uso y destruido al cerrar.
  Permite instalar un paquete y usarlo en el mismo flujo (CA-2).
- `reset()` destruye y recrea desde la imagen limpia (RF-EX-09).
- Límites: `--cpus`, `--memory`, `--pids-limit`, `--read-only` en raíz salvo `/home/agent`, `/tmp`
  y el workspace; `--cap-drop ALL`, `no-new-privileges`; timeout por comando.
- Workspace en el host: `var/workspaces/<session>/{in,out}`; `in` montado solo lectura.
- Red: el contenedor vive en `argos_sandbox` (`internal: true`, sin salida). La única salida es el
  proxy `egress-proxy` (allowlist, RF-EX-04), configurado vía `HTTP(S)_PROXY`.
- Se invoca la CLI `docker` (no el SDK): menos dependencias y cada llamada es trivial de auditar.

## Consecuencias
- Estado arrastrado dentro de una sesión (intencionado); entre sesiones, limpio.
- `apt` requiere root: la imagen trae lo básico y `pip/npm/cargo` instalan en espacio de usuario.
