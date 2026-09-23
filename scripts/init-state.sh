#!/usr/bin/env bash
# Crea state/ con plantillas solo si faltan ficheros (seguro ejecutar muchas veces).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${STATE_DIR:-$ROOT/state}"
EX="$ROOT/state.example"

mkdir -p "$STATE/config" "$STATE/data/gopro/videos" "$STATE/data/gopro/photos"

if [[ ! -f "$STATE/.env" ]]; then
  cp "$EX/.env" "$STATE/.env"
  echo "[init-state] Creado $STATE/.env — edita SESSION_SECRET (openssl rand -hex 32)"
else
  echo "[init-state] OK $STATE/.env (ya existe, no se toca)"
fi

if [[ ! -f "$STATE/config/users.json" ]]; then
  cp "$EX/config/users.json.example" "$STATE/config/users.json"
  echo "[init-state] Creado $STATE/config/users.json — pon password_hash (hash_password.py)"
else
  echo "[init-state] OK $STATE/config/users.json (ya existe)"
fi

# Migración desde layout antiguo (config/ y .env en la raíz del repo)
if [[ -f "$ROOT/config/users.json" ]] && grep -q password_hash "$ROOT/config/users.json" 2>/dev/null; then
  if [[ ! -f "$STATE/config/users.json" ]] || ! grep -q password_hash "$STATE/config/users.json" 2>/dev/null; then
    cp "$ROOT/config/users.json" "$STATE/config/users.json"
    echo "[init-state] Migrado users.json desde $ROOT/config/"
  fi
fi
if [[ -f "$ROOT/.env" ]] && ! grep -q '^SESSION_SECRET=.\+' "$STATE/.env" 2>/dev/null; then
  while IFS= read -r line; do
    key="${line%%=*}"
    if grep -q "^${key}=" "$STATE/.env" 2>/dev/null; then continue; fi
    echo "$line" >> "$STATE/.env"
  done < <(grep -E '^(SESSION_SECRET|PORTAL_BRAND|COOKIE_|AUTH_)=' "$ROOT/.env" 2>/dev/null || true)
  echo "[init-state] Variables de $ROOT/.env fusionadas en state/.env (revisa)"
fi
if [[ -d "$ROOT/data/gopro/videos" ]] && [[ -z "$(ls -A "$STATE/data/gopro/videos" 2>/dev/null)" ]]; then
  if compgen -G "$ROOT/data/gopro/videos/*" >/dev/null 2>&1; then
    echo "[init-state] Hay vídeos en data/gopro/ antiguo — muévelos a $STATE/data/gopro/videos/"
  fi
fi

echo "[init-state] STATE_DIR=$STATE"
