"""Tailnet-Forward-Overlay (docker-compose.tailnet-forward.yml): Platzhalter der Vorlagen,
envsubst-Filter und Unix-Socket zwischen Host- und Netz-Seite."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OVERLAY = PROJECT_ROOT / "docker-compose.tailnet-forward.yml"
TEMPLATES = PROJECT_ROOT / "deploy" / "tailnet-forward"
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
SOCKET = "/run/tailnet-forward/forward.sock"


def _service(name: str) -> str:
    text = OVERLAY.read_text(encoding="utf-8")
    block = text.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z-]+:\n|\nvolumes:\n", block, maxsplit=1)[0]


def _filter(service: str) -> set[str]:
    match = re.search(r"NGINX_ENVSUBST_FILTER: \^\(([A-Z0-9_|]+)\)\$\$", service)
    assert match, "NGINX_ENVSUBST_FILTER fehlt"
    return set(match.group(1).split("|"))


def _placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall((TEMPLATES / template).read_text(encoding="utf-8")))


def test_each_side_substitutes_exactly_its_template_placeholders():
    for service, template in (
        ("tailnet-forward-host", "host.conf.template"),
        ("tailnet-forward", "network.conf.template"),
    ):
        block = _service(service)
        placeholders = _placeholders(template)
        assert placeholders and placeholders <= _filter(block)
        for name in placeholders:
            assert f"{name}: ${{{name}:?" in block
        assert f"./deploy/tailnet-forward/{template}:/etc/nginx/templates/nginx.conf.template" in (
            block
        )
        assert "NGINX_ENVSUBST_OUTPUT_DIR: /etc/nginx" in block


def test_host_side_runs_in_host_network_and_network_side_carries_alias():
    host = _service("tailnet-forward-host")
    network = _service("tailnet-forward")
    assert "network_mode: host" in host
    assert "network_mode" not in network
    assert "- ${TAILNET_FORWARD_HOST:?" in network.split("aliases:", 1)[1]
    # Die Host-Seite öffnet keinen TCP-Port, sie lauscht nur am Socket.
    host_template = (TEMPLATES / "host.conf.template").read_text(encoding="utf-8")
    assert f"listen unix:{SOCKET};" in host_template
    assert f"proxy_pass unix:{SOCKET};" in (TEMPLATES / "network.conf.template").read_text(
        encoding="utf-8"
    )
    # Liegengebliebener Socket blockiert sonst den Neustart.
    assert f"rm -f {SOCKET}" in host
