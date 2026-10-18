"""nginx-Overlay (docker-compose.nginx.yml): Platzhalter der Vorlagen, envsubst-Filter und
weitere Domains der Instanz (PUBLIC_DOMAIN_ALIASES) mit eigenem Zertifikat."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "deploy" / "nginx" / "templates"
NGINX_OVERLAY = PROJECT_ROOT / "docker-compose.nginx.yml"
WEBSITE_OVERLAY = PROJECT_ROOT / "docker-compose.website.yml"
DEMO_PROXY_OVERLAY = PROJECT_ROOT / "docker-compose.demo-proxy.yml"

# Großgeschriebene ${…}-Platzhalter ersetzt envsubst; nginx-Variablen sind klein.
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _placeholders(name: str) -> set[str]:
    return set(_PLACEHOLDER.findall((TEMPLATES / name).read_text(encoding="utf-8")))


def _envsubst_filter(overlay: Path) -> set[str]:
    match = re.search(r"NGINX_ENVSUBST_FILTER: \^\(([A-Z0-9_|]+)\)\$\$", overlay.read_text())
    assert match, f"{overlay.name}: NGINX_ENVSUBST_FILTER fehlt"
    return set(match.group(1).split("|"))


def _nginx_environment(overlay: Path) -> str:
    text = overlay.read_text(encoding="utf-8")
    return text.split("  nginx:", 1)[1].split("\n  mcp:", 1)[0].split("\n  certbot:", 1)[0]


def test_envsubst_filter_covers_every_template_placeholder():
    # Nicht gefilterte Platzhalter blieben als ${…} stehen und brächen den nginx-Start.
    assert _placeholders("default.conf.template") <= _envsubst_filter(NGINX_OVERLAY)
    assert _placeholders("default.conf.template") | _placeholders(
        "website.conf.template"
    ) <= _envsubst_filter(WEBSITE_OVERLAY)
    # Das Demo-Proxy-Overlay kann hinter dem Webseiten-Overlay stehen und ersetzt dessen
    # Filter: alle drei Vorlagen abdecken.
    assert _placeholders("default.conf.template") | _placeholders(
        "website.conf.template"
    ) | _placeholders("demo.conf.template") <= _envsubst_filter(DEMO_PROXY_OVERLAY)


def test_aliases_are_always_set_for_nginx_and_certbot():
    # envsubst ersetzt nur gesetzte Variablen: leer setzen statt weglassen.
    overlay = NGINX_OVERLAY.read_text(encoding="utf-8")
    assert "PUBLIC_DOMAIN_ALIASES" in _nginx_environment(NGINX_OVERLAY)
    certbot = overlay.split("\n  certbot:", 1)[1]
    assert "PUBLIC_DOMAIN_ALIASES: ${PUBLIC_DOMAIN_ALIASES:-}" in certbot
    assert overlay.count("PUBLIC_DOMAIN_ALIASES: ${PUBLIC_DOMAIN_ALIASES:-}") == 2


def test_aliases_serve_the_instance_with_their_own_certificate():
    template = (TEMPLATES / "default.conf.template").read_text(encoding="utf-8")
    instance = template.split("server_name ${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES};")
    # Port 80 (Umleitung auf HTTPS unter demselben Namen) und Port 443 (die Instanz).
    assert len(instance) == 3
    http_block, https_block = instance[1].split("server {", 1)[0], instance[2]
    assert "return 301 https://$host$request_uri;" in http_block
    assert "ssl_certificate     /etc/letsencrypt/live/$ssl_server_name/fullchain.pem;" in (
        https_block
    )
    assert "ssl_certificate_cache" in https_block
    assert "proxy_pass http://obh_app;" in https_block
    # Kein Block leitet eine Alias-Domain auf PUBLIC_DOMAIN um.
    assert "return 301 https://${PUBLIC_DOMAIN}" not in https_block
    # Unbekannte Namen erreichen den Block mit Variablen-Zertifikat nicht.
    assert "ssl_reject_handshake on;" in template


def test_mcp_oauth_hint_follows_requested_domain():
    mcp = NGINX_OVERLAY.read_text(encoding="utf-8").split("\n  mcp:", 1)[1].split("\n  certbot:")[0]
    assert "MCP_PUBLIC_URL: ${MCP_PUBLIC_URL:-}" in mcp


def test_certbot_and_reload_hook_handle_every_instance_domain():
    certbot = (PROJECT_ROOT / "deploy" / "nginx" / "certbot.sh").read_text(encoding="utf-8")
    assert (
        'INSTANCE_DOMAINS="${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-} ${DEMO_DOMAIN:-}"'
        in certbot
    )
    # Platzhalter, erstes Zertifikat und `obtain` laufen über alle Instanz-Domains.
    assert certbot.count("for domain in ${INSTANCE_DOMAINS}; do") == 3
    # Variablen-Zertifikate lädt der nginx-Worker: Gruppe nginx muss lesen dürfen,
    # nach Platzhaltern, nach `obtain` und nach jeder Erneuerung.
    assert "NGINX_GID=101" in certbot
    assert certbot.count("share_with_nginx\n") == 3
    renew = certbot.split('certbot renew --webroot -w "${WEBROOT}" --quiet || true', 1)[1]
    assert renew.lstrip().startswith("share_with_nginx")
    hook = (PROJECT_ROOT / "deploy" / "nginx" / "nginx-cert-watch.sh").read_text(encoding="utf-8")
    assert (
        "for domain in ${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-} ${DEMO_DOMAIN:-}; do" in hook
    )


def test_demo_proxy_forwards_to_the_demo_project_at_runtime():
    overlay = DEMO_PROXY_OVERLAY.read_text(encoding="utf-8")
    # nginx bleibt im eigenen Netz und tritt zusätzlich dem Netz des Demo-Projekts bei.
    assert "      - default\n      - demo\n" in overlay
    assert "name: ${DEMO_NETWORK:-obh-demo_default}" in overlay
    assert "external: true" in overlay
    # certbot holt das Zertifikat der Demo-Domain.
    assert "DEMO_DOMAIN: ${DEMO_DOMAIN:?DEMO_DOMAIN must be set}" in overlay.split(
        "\n  certbot:", 1
    )[1]
    template = (TEMPLATES / "demo.conf.template").read_text(encoding="utf-8")
    # Laufzeit-Auflösung: nginx startet auch, wenn die Demo gestoppt ist.
    assert "resolver 127.0.0.11" in template
    assert "set $obh_demo http://${DEMO_UPSTREAM};" in template
    # Demo-MCP: /mcp geht an den MCP-Server des Demo-Projekts, ebenfalls zur Laufzeit.
    assert "DEMO_MCP_UPSTREAM: ${DEMO_MCP_UPSTREAM:-obh-demo-mcp-1:8090}" in overlay
    assert "|DEMO_MCP_UPSTREAM)$$" in overlay
    assert "set $obh_demo_mcp http://${DEMO_MCP_UPSTREAM};" in template
    mcp_location = template.split("location = /mcp {", 1)[1].split("\n    }", 1)[0]
    assert "proxy_pass $obh_demo_mcp;" in mcp_location
    assert 'proxy_set_header Origin "";' in mcp_location
    assert "proxy_set_header X-Forwarded-Host $host;" in mcp_location
    assert "proxy_pass http://obh_app;" not in template
    assert "ssl_certificate     /etc/letsencrypt/live/${DEMO_DOMAIN}/fullchain.pem;" in template
    assert "limit_req zone=obh_login" in template


def test_nginx_reaches_its_own_instance_by_unique_alias():
    # Mit dem Demo-Proxy hängt nginx auch im Netz der Demo, deren App ebenfalls „app“
    # heißt: Dienstnamen wären mehrdeutig (eine Nachbarinstanz landete so auf der Demo).
    template = (TEMPLATES / "default.conf.template").read_text(encoding="utf-8")
    assert "server obh-instance-app:8000;" in template
    assert "set $obh_mcp http://obh-instance-mcp:8090;" in template
    assert not re.search(r"\b(?<!-)(app:8000|mcp:8090)\b", template)
    overlay = NGINX_OVERLAY.read_text(encoding="utf-8")
    for alias in ("obh-instance-app", "obh-instance-mcp"):
        assert f"        aliases:\n          - {alias}\n" in overlay
