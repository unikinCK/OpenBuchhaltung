#!/usr/bin/env bash
#
# redeploy.sh — OpenBuchhaltung produktiv neu ausrollen.
#
# Arbeitet ausschließlich mit docker-compose.production.yml (gunicorn, PostgreSQL,
# fail-fast bei fehlenden Secrets). Ablauf:
#
#   1. git pull --ff-only, Ausgabe von Commit und Tag des neuen Standes
#   2. Backup der laufenden Instanz (backup.sh) — vor dem Stoppen
#   3. compose down, compose build (GIT_COMMIT als Build-Arg → Health/Manifest)
#   4. Eigentümer von instance/ im Volume auf den Container-Benutzer app setzen
#   5. alembic upgrade head als eigener, einmaliger Schritt (vor dem Start der Worker;
#      der app-Service selbst startet mit DB_AUTO_MIGRATE=0)
#   6. compose up -d, auf den Health-Check warten, Status ausgeben
#
# Optionen:
#   --skip-backup   kein Backup vor dem Redeploy (nur, wenn gerade eines gezogen wurde)
#   --no-pull       kein git pull (z. B. bereits ausgecheckter Release-Tag)
#   Alle weiteren Argumente werden an `compose up -d` durchgereicht.
#
# Umgebung: COMPOSE_FILE (Default docker-compose.production.yml), BACKUP_DIR,
#           BACKUP_KEEP (siehe backup.sh), APP_PORT (Default 8000).
#
set -euo pipefail

# Immer im Verzeichnis dieses Scripts (= Repo-Root) arbeiten.
cd "$(dirname "$0")"

COMPOSE_FILE_PATH="${COMPOSE_FILE:-docker-compose.production.yml}"
if [[ ! -f "$COMPOSE_FILE_PATH" ]]; then
  echo "Fehler: Compose-Datei '$COMPOSE_FILE_PATH' nicht gefunden." >&2
  exit 1
fi

# `docker compose` (v2) bevorzugen, sonst auf `docker-compose` (v1) zurückfallen.
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f "$COMPOSE_FILE_PATH")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose -f "$COMPOSE_FILE_PATH")
else
  echo "Fehler: weder 'docker compose' noch 'docker-compose' gefunden." >&2
  exit 1
fi

SKIP_BACKUP=0
DO_PULL=1
UP_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --skip-backup) SKIP_BACKUP=1 ;;
    --no-pull) DO_PULL=0 ;;
    *) UP_ARGS+=("$arg") ;;
  esac
done

if [[ $DO_PULL -eq 1 ]]; then
  echo ">>> git pull --ff-only"
  git pull --ff-only
fi

GIT_COMMIT="$(git rev-parse HEAD)"
GIT_DESCRIBE="$(git describe --tags --always --dirty 2>/dev/null || echo "$GIT_COMMIT")"
GIT_TAG="$(git describe --tags --exact-match 2>/dev/null || true)"
export GIT_COMMIT
echo ">>> Stand: Commit ${GIT_COMMIT} (${GIT_DESCRIBE})${GIT_TAG:+ — Release-Tag ${GIT_TAG}}"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "    Achtung: Arbeitsverzeichnis enthält lokale Änderungen." >&2
fi

if [[ $SKIP_BACKUP -eq 1 ]]; then
  echo ">>> Backup übersprungen (--skip-backup)"
elif "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx db; then
  echo ">>> Backup der laufenden Instanz (backup.sh)"
  COMPOSE_FILE="$COMPOSE_FILE_PATH" ./backup.sh
else
  echo ">>> Kein laufender db-Container — Backup übersprungen (Erstinstallation?)"
fi

echo ">>> compose down"
"${COMPOSE[@]}" down

echo ">>> compose build (GIT_COMMIT=${GIT_COMMIT})"
"${COMPOSE[@]}" build

echo ">>> Eigentümer von /app/instance im Volume auf app:app setzen"
"${COMPOSE[@]}" run --rm --no-deps --user root app \
  sh -c 'find /app/instance ! -user app -exec chown app:app {} + 2>/dev/null || true'

echo ">>> Migration: alembic upgrade head (eigener Schritt vor dem Start der Worker)"
"${COMPOSE[@]}" run --rm app alembic upgrade head

echo ">>> compose up -d"
"${COMPOSE[@]}" up -d ${UP_ARGS[@]+"${UP_ARGS[@]}"}

echo ">>> warte auf Health-Check des app-Containers"
APP_CONTAINER="$("${COMPOSE[@]}" ps -q app)"
HEALTH="unbekannt"
for _ in $(seq 1 40); do
  HEALTH="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$APP_CONTAINER" 2>/dev/null || echo unbekannt)"
  case "$HEALTH" in
    healthy) break ;;
    unhealthy) break ;;
  esac
  sleep 3
done
echo ">>> Health: ${HEALTH}"
if command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time 5 "http://127.0.0.1:${APP_PORT:-8000}/api/v1/health" && echo
fi
if [[ "$HEALTH" != "healthy" ]]; then
  echo "Fehler: app-Container ist nicht healthy — Logs prüfen: ${COMPOSE[*]} logs app" >&2
  "${COMPOSE[@]}" ps
  exit 1
fi

echo ">>> fertig — Status:"
"${COMPOSE[@]}" ps
