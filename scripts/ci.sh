#!/usr/bin/env bash
# CI local: lint, tests y suite dorada con el proveedor simulado (sin modelo, sin coste).
# Ejecuta todos los pasos aunque alguno falle y resume al final; sale con 1 si falla alguno.
# Uso: scripts/ci.sh [--no-eval]
# Las tareas de sandbox de `golden` necesitan el broker en marcha (docker compose up -d broker);
# sin él se omiten. Sus sesiones quedan auditadas en el segmento main con canal `eval`.
set -uo pipefail
cd "$(dirname "$0")/.."

run_eval=1
for arg in "$@"; do
  case "$arg" in
    --no-eval) run_eval=0 ;;
    *) echo "uso: $0 [--no-eval]" >&2; exit 2 ;;
  esac
done

results=()
failed=0

step() {
  local name=$1; shift
  echo -e "\n\033[1m▶ $name\033[0m"
  local start=$SECONDS
  if "$@"; then
    results+=("✅ $name ($((SECONDS - start)) s)")
  else
    results+=("❌ $name ($((SECONDS - start)) s)")
    failed=1
  fi
}

step "ruff check" uv run ruff check .
step "ruff format" uv run ruff format --check .
step "pytest" uv run pytest -q
if ((run_eval)); then
  step "eval golden (fake)" uv run argos eval run golden --provider fake
fi

echo -e "\n\033[1mResumen CI\033[0m"
printf '  %s\n' "${results[@]}"
exit "$failed"
