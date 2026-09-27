#!/usr/bin/env bash
# Monta Argos dentro de la sesión Herdr actual (ejecútalo DESDE un pane de Herdr):
#   - arranca/asegura el núcleo persistente (servicio compose `daemon`, segmento main);
#   - abre a la derecha un pane con la consola de operador (`argos console`), que informa a Herdr
#     de su estado (working / blocked por aprobación / idle) y notifica las aprobaciones;
#   - abre debajo un pane con los logs del núcleo.
# El pane actual queda libre para enviar tareas: scripts/argos core submit "…"
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${HERDR_ENV:-}" != 1 ]]; then
  echo "No estás dentro de un pane de Herdr (HERDR_ENV no es 1). Abre Herdr y ejecútalo desde ahí." >&2
  exit 1
fi
command -v jq >/dev/null || { echo "Falta jq" >&2; exit 1; }

docker compose up -d egress-main >/dev/null
docker compose --profile daemon up -d daemon >/dev/null
for _ in $(seq 1 50); do [[ -S var/segments/main/run/argos.sock ]] && break; sleep 0.2; done

console_pane=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus \
  | jq -r '.result.pane.pane_id')
herdr pane rename "$console_pane" "argos · consola" >/dev/null
herdr pane run "$console_pane" "uv run argos console"

logs_pane=$(herdr pane split "$console_pane" --direction down --ratio 0.35 --cwd "$PWD" --no-focus \
  | jq -r '.result.pane.pane_id')
herdr pane rename "$logs_pane" "argos · núcleo" >/dev/null
herdr pane run "$logs_pane" "docker compose --profile daemon logs -f daemon"

echo "Argos listo: consola en $console_pane, logs en $logs_pane."
echo "Envía tareas desde aquí:  scripts/argos core submit \"…\"   (o --detach)"
