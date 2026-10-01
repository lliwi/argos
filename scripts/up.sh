#!/usr/bin/env bash
# Levanta el núcleo persistente de Argos (daemon + puente Matrix, segmento main).
# Uso: scripts/up.sh [--prod] [args extra de `docker compose up`]
#   --prod  aplica compose.prod.yaml: IP macvlan propia (int-lan) y API TCP con TLS + token
#           (ADR-0025). Requiere `argos api-setup <ip>` una vez.
set -euo pipefail
cd "$(dirname "$0")/.."

files=(-f compose.yaml)
if [[ "${1:-}" == "--prod" ]]; then
  shift
  for f in secrets/api.env secrets/api-cert.pem secrets/api-key.pem; do
    [[ -f "$f" ]] || { echo "Falta $f: ejecuta 'uv run argos api-setup <ip>'" >&2; exit 1; }
  done
  net="${ARGOS_LAN_NETWORK:-int-lan}"
  docker network inspect "$net" >/dev/null 2>&1 \
    || { echo "No existe la red docker '$net' (macvlan de la LAN)" >&2; exit 1; }
  files+=(-f compose.prod.yaml)
fi

docker compose "${files[@]}" up -d egress-main broker
docker compose "${files[@]}" --profile daemon up -d "$@" daemon matrix
docker compose "${files[@]}" --profile daemon ps daemon matrix
