#!/usr/bin/env bash
# Construye las imágenes de Argos cada tag UNA sola vez.
#
# Varios servicios comparten el mismo `image:` (egress-main/osint/pentest => argos-egress:latest;
# core/daemon/matrix… => argos-core:latest). Si se construyen a la vez (p. ej. `docker compose
# --profile core build`), el image store de containerd aborta con "AlreadyExists": dos builds del
# mismo tag chocan. Aquí se nombra un único servicio por imagen, en serie, para evitar la carrera.
#
# Uso: scripts/build.sh [--kali] [args extra de `docker compose build`]
#   --kali  construye también la imagen de Kali (solo necesaria para el segmento pentest).
set -euo pipefail
cd "$(dirname "$0")/.."

kali=0
[[ "${1:-}" == "--kali" ]] && { kali=1; shift; }

echo "==> argos-egress:latest"
docker compose build "$@" egress-main
echo "==> argos-core:latest"
docker compose --profile core build "$@" core
echo "==> argos-sandbox:latest"
docker compose --profile build build "$@" sandbox-image
if [[ "$kali" == 1 ]]; then
  echo "==> argos-kali:latest"
  docker compose --profile kali build "$@" kali
fi
echo "Listo. Levanta con scripts/up.sh [--prod]."
