#!/usr/bin/env bash
# Genera .env para docker compose (UID/GID, grupo docker, rutas) y crea los directorios de
# credenciales con tu usuario como propietario (si los creara Docker, serían de root).
set -euo pipefail
cd "$(dirname "$0")/.."
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/argos"
mkdir -p "$CFG/codex-home"
for s in main osint pentest; do
  mkdir -p "var/segments/$s/run" "var/egress/$s" "var/broker/$s"
  chmod 700 "var/segments/$s/run"   # socket de la API del daemon de cada segmento (ADR-0026)
done
chmod 700 "$CFG" "$CFG/codex-home"
cat > .env <<ENV
UID=$(id -u)
GID=$(id -g)
DOCKER_GID=$(stat -c %g /var/run/docker.sock)
ARGOS_DIR=$(pwd)
ARGOS_CODEX_HOME=$CFG/codex-home
ARGOS_AGE_DIR=$CFG
ENV
echo "Escrito .env:"; cat .env
