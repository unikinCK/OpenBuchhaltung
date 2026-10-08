#!/bin/sh
# nginx-Entrypoint-Hook des nginx-Overlays: wartet auf das (Platzhalter-)Zertifikat
# vom certbot-Service und lädt nginx neu, sobald sich das Zertifikat ändert
# (erstes Let's-Encrypt-Zertifikat, Erneuerungen).
set -eu

CERT="/etc/letsencrypt/live/${PUBLIC_DOMAIN}/fullchain.pem"

i=0
until [ -f "${CERT}" ]; do
  i=$((i + 1))
  if [ "${i}" -gt 120 ]; then
    echo "$0: kein Zertifikat unter ${CERT} — läuft der certbot-Service?" >&2
    exit 1
  fi
  sleep 1
done

(
  last="$(stat -L -c %Y "${CERT}" 2>/dev/null || echo 0)"
  while :; do
    sleep 60
    now="$(stat -L -c %Y "${CERT}" 2>/dev/null || echo "${last}")"
    if [ "${now}" != "${last}" ] && [ -f "${CERT}" ]; then
      echo "$0: Zertifikat geändert — nginx -s reload"
      nginx -s reload || true
      last="${now}"
    fi
  done
) >/proc/1/fd/1 2>&1 &
