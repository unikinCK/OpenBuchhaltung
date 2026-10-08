#!/bin/sh
# certbot-Service des nginx-Overlays (docker-compose.nginx.yml).
#
#   sh /opt/certbot.sh          Dauerbetrieb: Platzhalter anlegen, Zertifikat holen,
#                               danach regelmäßig erneuern
#   sh /opt/certbot.sh obtain   einmalig das Let's-Encrypt-Zertifikat holen
#
# Umgebung: PUBLIC_DOMAIN (Pflicht), CERTBOT_EMAIL (Kontakt für Ablaufwarnungen),
#           CERTBOT_STAGING=1 (Test-CA, keine Rate-Limits, Browser warnt).
set -eu

: "${PUBLIC_DOMAIN:?PUBLIC_DOMAIN must be set}"
LIVE="/etc/letsencrypt/live/${PUBLIC_DOMAIN}"
WEBROOT=/var/www/certbot

# Selbstsigniertes Zertifikat, damit nginx vor dem ersten echten Zertifikat startet.
dummy_cert() {
  [ -f "${LIVE}/fullchain.pem" ] && return 0
  echo "certbot: lege Platzhalter-Zertifikat für ${PUBLIC_DOMAIN} an"
  mkdir -p "${LIVE}"
  openssl req -x509 -nodes -newkey rsa:2048 -days 7 \
    -keyout "${LIVE}/privkey.pem" -out "${LIVE}/fullchain.pem" \
    -subj "/CN=${PUBLIC_DOMAIN}" >/dev/null 2>&1
  touch "${LIVE}/.dummy"
}

obtain() {
  set -- certonly --webroot -w "${WEBROOT}" --cert-name "${PUBLIC_DOMAIN}" \
    -d "${PUBLIC_DOMAIN}" --agree-tos --non-interactive --keep-until-expiring
  if [ -n "${CERTBOT_EMAIL:-}" ]; then
    set -- "$@" --email "${CERTBOT_EMAIL}"
  else
    set -- "$@" --register-unsafely-without-email
  fi
  [ "${CERTBOT_STAGING:-0}" = "1" ] && set -- "$@" --staging
  # Der Platzhalter belegt live/<domain>; certbot würde sonst <domain>-0001 anlegen.
  # nginx hält das alte Zertifikat im Speicher und lädt erst bei Änderung neu.
  if [ -f "${LIVE}/.dummy" ]; then
    mkdir -p /tmp/dummy && cp "${LIVE}"/*.pem /tmp/dummy/
    rm -rf "${LIVE}"
  fi
  if certbot "$@"; then
    echo "certbot: Zertifikat für ${PUBLIC_DOMAIN} vorhanden"
    return 0
  fi
  if [ ! -f "${LIVE}/fullchain.pem" ] && [ -d /tmp/dummy ]; then
    mkdir -p "${LIVE}" && cp /tmp/dummy/*.pem "${LIVE}/" && touch "${LIVE}/.dummy"
  fi
  echo "certbot: Zertifikat konnte nicht ausgestellt werden (DNS-Record und Weiterleitung von Port 80 prüfen)" >&2
  return 1
}

if [ "${1:-}" = "obtain" ]; then
  obtain
  exit $?
fi

trap 'exit 0' TERM INT
mkdir -p "${WEBROOT}"
dummy_cert
while :; do
  if [ -f "${LIVE}/.dummy" ]; then
    # Höchstens ein Versuch je Stunde — Let's Encrypt erlaubt 5 Fehlversuche/Stunde.
    obtain || true
    [ -f "${LIVE}/.dummy" ] && wait_for=1h || wait_for=12h
  else
    certbot renew --webroot -w "${WEBROOT}" --quiet || true
    wait_for=12h
  fi
  sleep "${wait_for}" &
  wait $!
done
