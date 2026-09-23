#!/usr/bin/env bash
# Actualiza código desde Git; no modifica state/ (config, vídeos, .env).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
"$ROOT/scripts/init-state.sh"
git pull --rebase
docker compose build
docker compose up -d
echo "[upgrade] Listo. Config en: ${STATE_DIR:-$ROOT/state}"
