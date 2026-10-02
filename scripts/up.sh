#!/usr/bin/env bash
# Levanta los núcleos persistentes de Argos: main (daemon + puente Matrix) y osint (ADR-0026).
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

# Sockets de control de cada segmento: si los creara Docker al montarlos, serían de root.
for s in main osint pentest; do mkdir -p "var/segments/$s/run" && chmod 700 "var/segments/$s/run"; done

docker compose "${files[@]}" up -d egress-main egress-osint broker
docker compose "${files[@]}" --profile daemon up -d "$@" daemon daemon-osint matrix
docker compose "${files[@]}" --profile daemon ps daemon daemon-osint matrix
echo "pentest (bajo demanda): docker compose --profile pentest --profile kali up -d daemon-pentest kali egress-pentest"
