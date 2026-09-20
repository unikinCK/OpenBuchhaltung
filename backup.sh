#!/usr/bin/env bash
#
# backup.sh — Sicherung der produktiven OpenBuchhaltung-Instanz (Compose-Stack).
#
# Legt unter BACKUP_DIR (Default ./backups) ein Verzeichnis
# openbuchhaltung-<JJJJMMTT-HHMMSS>/ an mit:
#
#   db.dump           pg_dump der PostgreSQL-Datenbank (Custom-Format, komprimiert)
#   instance.tar.gz   Belegablage und Laufzeitdaten (/app/instance aus dem app_data-Volume)
#   meta.txt          Zeitpunkt, Git-Commit/-Tag, Alembic-Revision, Image, Compose-Datei
#   SHA256SUMS        Prüfsummen aller Dateien
#
# Aufbewahrung: die letzten BACKUP_KEEP (Default 14) Sicherungen bleiben erhalten.
# Voraussetzung: der db-Container des Stacks läuft. Das Ergebnis gehört verschlüsselt
# und getrennt vom Produktivsystem abgelegt (docs/compliance/produktionsbetrieb.md §6,
# dort auch das Restore-Runbook).
#
# Umgebung: COMPOSE_FILE (Default docker-compose.production.yml), BACKUP_DIR,
#           BACKUP_KEEP, POSTGRES_USER/POSTGRES_DB (Default openbuchhaltung).
#
set -euo pipefail
umask 077

cd "$(dirname "$0")"

COMPOSE_FILE_PATH="${COMPOSE_FILE:-docker-compose.production.yml}"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f "$COMPOSE_FILE_PATH")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose -f "$COMPOSE_FILE_PATH")
else
  echo "Fehler: weder 'docker compose' noch 'docker-compose' gefunden." >&2
  exit 1
fi

BACKUP_ROOT="${BACKUP_DIR:-./backups}"
KEEP="${BACKUP_KEEP:-14}"
DB_USER="${POSTGRES_USER:-openbuchhaltung}"
DB_NAME="${POSTGRES_DB:-openbuchhaltung}"
STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_ROOT}/openbuchhaltung-${STAMP}"

if ! "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx db; then
  echo "Fehler: der db-Container läuft nicht (${COMPOSE[*]} ps)." >&2
  exit 1
fi

mkdir -p "$TARGET"
echo ">>> Backup nach ${TARGET}"

echo ">>> pg_dump ${DB_NAME} → db.dump"
"${COMPOSE[@]}" exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --compress=6 \
  > "${TARGET}/db.dump"

ALEMBIC_REVISION="$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -Atc \
  'SELECT version_num FROM alembic_version' 2>/dev/null || echo unbekannt)"
POSTGRES_VERSION="$("${COMPOSE[@]}" exec -T db psql -U "$DB_USER" -d "$DB_NAME" -Atc \
  'SHOW server_version' 2>/dev/null || echo unbekannt)"

echo ">>> Belegablage /app/instance → instance.tar.gz"
"${COMPOSE[@]}" run --rm --no-deps -T app tar -C /app -czf - instance \
  > "${TARGET}/instance.tar.gz"

APP_CONTAINER="$("${COMPOSE[@]}" ps -q app 2>/dev/null || true)"
APP_IMAGE="unbekannt"
if [[ -n "$APP_CONTAINER" ]]; then
  APP_IMAGE="$(docker inspect --format '{{.Config.Image}} ({{.Image}})' "$APP_CONTAINER" 2>/dev/null || echo unbekannt)"
fi

cat > "${TARGET}/meta.txt" <<META
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
host=$(hostname)
compose_file=${COMPOSE_FILE_PATH}
git_commit=$(git rev-parse HEAD 2>/dev/null || echo unbekannt)
git_describe=$(git describe --tags --always --dirty 2>/dev/null || echo unbekannt)
alembic_revision=${ALEMBIC_REVISION}
postgres_version=${POSTGRES_VERSION}
database=${DB_NAME}
app_image=${APP_IMAGE}
META

(
  cd "$TARGET"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum db.dump instance.tar.gz meta.txt > SHA256SUMS
  else
    shasum -a 256 db.dump instance.tar.gz meta.txt > SHA256SUMS
  fi
)

echo ">>> Inhalt:"
ls -la "$TARGET"
cat "${TARGET}/meta.txt"

# Aufbewahrung: alle bis auf die letzten KEEP Sicherungen entfernen.
if [[ "$KEEP" =~ ^[0-9]+$ ]] && [[ "$KEEP" -gt 0 ]]; then
  ALL=()
  while IFS= read -r dir; do
    ALL+=("$dir")
  done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'openbuchhaltung-*' | sort)
  COUNT=${#ALL[@]}
  if (( COUNT > KEEP )); then
    for (( i = 0; i < COUNT - KEEP; i++ )); do
      echo ">>> entferne altes Backup ${ALL[$i]}"
      rm -rf "${ALL[$i]}"
    done
  fi
fi

echo ">>> Backup abgeschlossen: ${TARGET}"
