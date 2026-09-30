#!/usr/bin/env bash
# Obtiene un access token para la cuenta del bot y lo guarda en secrets/matrix.env (600, fuera de git).
# La contraseña se pide sin eco y no se guarda en ningún sitio.
# Uso: scripts/matrix-login.sh https://matrix.tudominio.org argos
set -euo pipefail
cd "$(dirname "$0")/.."
HS="${1:?uso: $0 <homeserver> <usuario>}"
USER_NAME="${2:?uso: $0 <homeserver> <usuario>}"
read -rsp "Contraseña de ${USER_NAME}: " PASS; echo
body=$(jq -n --arg u "$USER_NAME" --arg p "$PASS" \
  '{type:"m.login.password", identifier:{type:"m.id.user", user:$u}, password:$p,
    initial_device_display_name:"argos"}')
unset PASS
resp=$(curl -fsS -X POST "${HS%/}/_matrix/client/v3/login" -H 'Content-Type: application/json' \
  --data-binary @- <<<"$body") || { echo "login fallido" >&2; exit 1; }
token=$(jq -r .access_token <<<"$resp")
user_id=$(jq -r .user_id <<<"$resp")
umask 077
printf 'ARGOS_MATRIX_TOKEN=%s\n' "$token" > secrets/matrix.env
echo "Token guardado en secrets/matrix.env para ${user_id}."
echo "En config/argos.yaml: matrix.homeserver=${HS}  matrix.user_id=\"${user_id}\"  y allowed_users."
