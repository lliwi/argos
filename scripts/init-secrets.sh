#!/usr/bin/env bash
# Genera una clave age local (fuera del repo) y escribe .sops.yaml con su clave pública.
# Uso: scripts/init-secrets.sh            Luego: sops secrets/infra.sops.yaml
set -euo pipefail
KEY_DIR="${ARGOS_KEY_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/argos}"
KEY_FILE="$KEY_DIR/age.key"
command -v age-keygen >/dev/null || { echo "Falta age (p.ej. pacman -S age)"; exit 1; }
command -v sops >/dev/null || { echo "Falta sops (p.ej. pacman -S sops)"; exit 1; }
mkdir -p "$KEY_DIR"; chmod 700 "$KEY_DIR"
if [[ ! -f "$KEY_FILE" ]]; then
  age-keygen -o "$KEY_FILE"; chmod 600 "$KEY_FILE"
fi
PUB=$(age-keygen -y "$KEY_FILE")
cat > "$(dirname "$0")/../.sops.yaml" <<YAML
creation_rules:
  - path_regex: secrets/.*\.sops\.yaml$
    age: $PUB
YAML
echo "Listo. Clave en $KEY_FILE. Edita: sops secrets/<perfil>.sops.yaml"
