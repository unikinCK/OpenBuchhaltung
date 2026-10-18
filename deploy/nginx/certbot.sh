#!/bin/sh
# certbot-Service des nginx-Overlays (docker-compose.nginx.yml).
#
#   sh /opt/certbot.sh          Dauerbetrieb: Platzhalter anlegen, Zertifikate holen,
#                               danach regelmäßig erneuern
#   sh /opt/certbot.sh obtain   einmalig die Let's-Encrypt-Zertifikate holen
#
# Umgebung: PUBLIC_DOMAIN (Pflicht), CERTBOT_EMAIL (Kontakt für Ablaufwarnungen),
#           CERTBOT_STAGING=1 (Test-CA, keine Rate-Limits, Browser warnt),
#           PUBLIC_DOMAIN_ALIASES (weitere Domains der Instanz, durch Leerzeichen
#           getrennt): je Domain ein eigenes Zertifikat,
#           WEBSITE_DOMAIN (Projekt-Webseite, docker-compose.website.yml): zweites
#           Zertifikat für die Domain und www.<Domain>,
#           DEMO_DOMAIN (Demo-Instanz, docker-compose.demo-proxy.yml): eigenes
#           Zertifikat wie eine Instanz-Domain.
set -eu

: "${PUBLIC_DOMAIN:?PUBLIC_DOMAIN must be set}"
WEBROOT=/var/www/certbot
# Instanz-Domains: jede bekommt ein eigenes Zertifikat (keine Umleitung zwischen ihnen).
# Die Domain der Demo-Instanz wird genauso behandelt.
INSTANCE_DOMAINS="${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-} ${DEMO_DOMAIN:-}"

live_dir() {
  echo "/etc/letsencrypt/live/$1"
}

is_dummy() {
  [ -f "$(live_dir "$1")/.dummy" ]
}

# Die Instanz-Zertifikate wählt nginx per SNI über eine Variable; die lädt der
# Worker-Prozess (Benutzer nginx, GID 101 im nginx-Image) statt des root-Masters.
# Daher Lesezugriff über die Gruppe — der Worker hält die Schlüssel ohnehin im Speicher.
# Nach jedem Ausstellen und Erneuern, denn certbot legt neue Schlüssel mit 0600 an.
NGINX_GID=101
share_with_nginx() {
  for dir in /etc/letsencrypt/live /etc/letsencrypt/archive; do
    [ -d "${dir}" ] || continue
    chgrp -R "${NGINX_GID}" "${dir}"
    chmod -R g+rX "${dir}"
  done
}

# Selbstsigniertes Zertifikat, damit nginx vor dem ersten echten Zertifikat startet.
dummy_cert() {
  live="$(live_dir "$1")"
  [ -f "${live}/fullchain.pem" ] && return 0
  echo "certbot: lege Platzhalter-Zertifikat für $1 an"
  mkdir -p "${live}"
  openssl req -x509 -nodes -newkey rsa:2048 -days 7 \
    -keyout "${live}/privkey.pem" -out "${live}/fullchain.pem" \
    -subj "/CN=$1" >/dev/null 2>&1
  touch "${live}/.dummy"
}

# obtain <Domain> [weitere Domains …]: Zertifikat mit der ersten Domain als Namen.
obtain() {
  name="$1"
  domains="$*"
  live="$(live_dir "${name}")"
  backup="/tmp/dummy-${name}"
  set -- certonly --webroot -w "${WEBROOT}" --cert-name "${name}" \
    --agree-tos --non-interactive --keep-until-expiring
  for domain in ${domains}; do
    set -- "$@" -d "${domain}"
  done
  if [ -n "${CERTBOT_EMAIL:-}" ]; then
    set -- "$@" --email "${CERTBOT_EMAIL}"
  else
    set -- "$@" --register-unsafely-without-email
  fi
  [ "${CERTBOT_STAGING:-0}" = "1" ] && set -- "$@" --staging
  # Der Platzhalter belegt live/<domain>; certbot würde sonst <domain>-0001 anlegen.
  # nginx hält das alte Zertifikat im Speicher und lädt erst bei Änderung neu.
  if is_dummy "${name}"; then
    rm -rf "${backup}" && mkdir -p "${backup}" && cp "${live}"/*.pem "${backup}/"
    rm -rf "${live}"
  fi
  if certbot "$@"; then
    echo "certbot: Zertifikat für ${domains} vorhanden"
    return 0
  fi
  if [ ! -f "${live}/fullchain.pem" ] && [ -d "${backup}" ]; then
    mkdir -p "${live}" && cp "${backup}"/*.pem "${live}/" && touch "${live}/.dummy"
  fi
  echo "certbot: Zertifikat für ${domains} konnte nicht ausgestellt werden (DNS-Record und Weiterleitung von Port 80 prüfen)" >&2
  return 1
}

obtain_website() {
  obtain "${WEBSITE_DOMAIN}" "www.${WEBSITE_DOMAIN}"
}

if [ "${1:-}" = "obtain" ]; then
  status=0
  for domain in ${INSTANCE_DOMAINS}; do
    obtain "${domain}" || status=1
  done
  if [ -n "${WEBSITE_DOMAIN:-}" ]; then
    obtain_website || status=1
  fi
  share_with_nginx
  exit "${status}"
fi

trap 'exit 0' TERM INT
mkdir -p "${WEBROOT}"
for domain in ${INSTANCE_DOMAINS}; do
  dummy_cert "${domain}"
done
if [ -n "${WEBSITE_DOMAIN:-}" ]; then
  dummy_cert "${WEBSITE_DOMAIN}"
fi
share_with_nginx
while :; do
  # Höchstens ein Versuch je Stunde und Zertifikat — Let's Encrypt erlaubt
  # 5 Fehlversuche je Domain und Stunde.
  pending=0
  for domain in ${INSTANCE_DOMAINS}; do
    if is_dummy "${domain}"; then
      obtain "${domain}" || pending=1
    fi
  done
  if [ -n "${WEBSITE_DOMAIN:-}" ] && is_dummy "${WEBSITE_DOMAIN}"; then
    obtain_website || pending=1
  fi
  # Erneuert nur echte Zertifikate; Platzhalter haben keine Renewal-Konfiguration.
  certbot renew --webroot -w "${WEBROOT}" --quiet || true
  share_with_nginx
  if [ "${pending}" = "1" ]; then
    wait_for=1h
  else
    wait_for=12h
  fi
  sleep "${wait_for}" &
  wait $!
done
