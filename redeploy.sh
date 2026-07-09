#!/usr/bin/env bash
#
# redeploy.sh — OpenBuchhaltung neu ausrollen.
#
# Ablauf: git pull -> compose down -> compose build -> compose up -d
#
# Zusätzliche Argumente werden an `up` durchgereicht, z. B.:
#   ./redeploy.sh --profile proxy
#
set -euo pipefail

# Immer im Verzeichnis dieses Scripts (= Repo-Root) arbeiten.
cd "$(dirname "$0")"

# `docker compose` (v2) bevorzugen, sonst auf `docker-compose` (v1) zurückfallen.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Fehler: weder 'docker compose' noch 'docker-compose' gefunden." >&2
  exit 1
fi

echo ">>> git pull"
git pull

echo ">>> compose down"
"${COMPOSE[@]}" down

echo ">>> compose build"
"${COMPOSE[@]}" build

echo ">>> compose up -d"
"${COMPOSE[@]}" up -d "$@"

echo ">>> fertig — Status:"
"${COMPOSE[@]}" ps
