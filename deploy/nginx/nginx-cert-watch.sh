#!/bin/sh
# nginx-Entrypoint-Hook des nginx-Overlays: wartet auf die (Platzhalter-)Zertifikate
# vom certbot-Service und lädt nginx neu, sobald sich ein Zertifikat ändert
# (erstes Let's-Encrypt-Zertifikat, Erneuerungen). Dazu gehören die Zertifikate der
# weiteren Instanz-Domains (PUBLIC_DOMAIN_ALIASES) und mit dem Webseiten-Overlay
# (WEBSITE_DOMAIN) das der Projekt-Webseite, mit dem Demo-Proxy-Overlay (DEMO_DOMAIN)
# das der Demo-Instanz und mit dem Instanz-Proxy-Overlay (INSTANCE_PROXY_DOMAIN) das der
# weiteren Instanz.
set -eu

CERTS=""
for domain in ${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-} ${DEMO_DOMAIN:-} ${INSTANCE_PROXY_DOMAIN:-}; do
  CERTS="${CERTS} /etc/letsencrypt/live/${domain}/fullchain.pem"
done
if [ -n "${WEBSITE_DOMAIN:-}" ]; then
  CERTS="${CERTS} /etc/letsencrypt/live/${WEBSITE_DOMAIN}/fullchain.pem"
fi

for cert in ${CERTS}; do
  i=0
  until [ -f "${cert}" ]; do
    i=$((i + 1))
    if [ "${i}" -gt 120 ]; then
      echo "$0: kein Zertifikat unter ${cert} — läuft der certbot-Service?" >&2
      exit 1
    fi
    sleep 1
  done
done

all_present() {
  for cert in ${CERTS}; do
    [ -f "${cert}" ] || return 1
  done
}

# Änderungszeitpunkte aller Zertifikate in einer Zeile.
stamps() {
  for cert in ${CERTS}; do
    stat -L -c %Y "${cert}" 2>/dev/null || echo 0
  done | tr '\n' ' '
}

(
  last="$(stamps)"
  while :; do
    sleep 60
    now="$(stamps)"
    if [ "${now}" != "${last}" ] && all_present; then
      echo "$0: Zertifikat geändert — nginx -s reload"
      nginx -s reload || true
      last="${now}"
    fi
  done
) >/proc/1/fd/1 2>&1 &
