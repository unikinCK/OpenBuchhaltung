"""nginx-Overlay (docker-compose.nginx.yml): Platzhalter der Vorlagen, envsubst-Filter und
weitere Domains der Instanz (PUBLIC_DOMAIN_ALIASES) mit eigenem Zertifikat."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "deploy" / "nginx" / "templates"
NGINX_OVERLAY = PROJECT_ROOT / "docker-compose.nginx.yml"
WEBSITE_OVERLAY = PROJECT_ROOT / "docker-compose.website.yml"

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
    assert 'INSTANCE_DOMAINS="${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-}"' in certbot
    # Platzhalter, erstes Zertifikat und `obtain` laufen über alle Instanz-Domains.
    assert certbot.count("for domain in ${INSTANCE_DOMAINS}; do") == 3
    hook = (PROJECT_ROOT / "deploy" / "nginx" / "nginx-cert-watch.sh").read_text(encoding="utf-8")
    assert "for domain in ${PUBLIC_DOMAIN} ${PUBLIC_DOMAIN_ALIASES:-}; do" in hook
