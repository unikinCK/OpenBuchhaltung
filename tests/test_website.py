"""Projekt-Webseite (website/): Links und Ressourcen vollständig und lokal, Seiten tauglich
für die Content-Security-Policy des nginx-Overlays, Auslieferung über
docker-compose.website.yml."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WEBSITE = PROJECT_ROOT / "website"
PAGES = sorted(WEBSITE.glob("*.html"))
TEMPLATE = PROJECT_ROOT / "deploy" / "nginx" / "templates" / "website.conf.template"
OVERLAY = PROJECT_ROOT / "docker-compose.website.yml"


class _Page(HTMLParser):
    """Sammelt IDs, Verweise und alles, was die CSP betrifft."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.title = ""
        self.meta: dict[str, str] = {}
        self.ids: set[str] = set()
        # (Tag, Attribut, Wert, rel)
        self.refs: list[tuple[str, str, str, str]] = []
        self.aria_controls: list[str] = []
        self.style_tags = 0
        self.style_attributes: list[str] = []
        self.event_handlers: list[str] = []
        self.inline_scripts: list[tuple[dict[str, str | None], str]] = []
        self._in_title = False
        self._script: dict[str, str | None] | None = None
        self._script_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        rel = attributes.get("rel") or ""
        if tag == "html":
            self.lang = attributes.get("lang")
        if tag == "title":
            self._in_title = True
        if tag == "style":
            self.style_tags += 1
        if tag == "meta" and attributes.get("name"):
            self.meta[attributes["name"]] = attributes.get("content") or ""
        if tag == "script":
            self._script = attributes
            self._script_text = []
        for name, value in attrs:
            if name == "id" and value:
                self.ids.add(value)
            elif name == "style":
                self.style_attributes.append(tag)
            elif name.startswith("on"):
                self.event_handlers.append(f"{tag}[{name}]")
            elif name in {"href", "src"} and value:
                self.refs.append((tag, name, value, rel))
            elif name == "srcset" and value:
                for candidate in value.split(","):
                    self.refs.append((tag, name, candidate.split()[0], rel))
            elif name == "aria-controls" and value:
                self.aria_controls.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._script is not None:
            if "src" not in self._script:
                self.inline_scripts.append((self._script, "".join(self._script_text)))
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._script is not None:
            self._script_text.append(data)


def _parse(path: Path) -> _Page:
    page = _Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def _is_external(value: str) -> bool:
    return urlsplit(value).scheme in {"http", "https"} or value.startswith("//")


def _local_target(page_path: Path, value: str) -> Path:
    target = urlsplit(value).path
    # Absolute Pfade (z. B. in 404.html) beziehen sich auf die Wurzel der Webseite.
    base = WEBSITE / target.lstrip("/") if target.startswith("/") else page_path.parent / target
    if target in {"", "./", "/"} or target.endswith("/"):
        base = base / "index.html"
    return base.resolve()


@pytest.fixture(scope="module")
def pages() -> dict[Path, _Page]:
    assert PAGES, "website/ enthält keine HTML-Seiten"
    return {path: _parse(path) for path in PAGES}


# --- Seiten und Verweise --------------------------------------------------------


def test_website_contains_start_and_legal_pages():
    names = {path.name for path in PAGES}
    assert {"index.html", "impressum.html", "datenschutz.html", "404.html"} <= names


def test_pages_declare_language_title_and_viewport(pages):
    for path, page in pages.items():
        assert page.lang == "de", path.name
        assert page.title.strip(), path.name
        assert "viewport" in page.meta, path.name
        if path.name != "404.html":
            assert page.meta.get("description"), path.name


def test_local_links_and_assets_exist(pages):
    for path, page in pages.items():
        for _tag, _attr, value, _rel in page.refs:
            if _is_external(value) or value.startswith("mailto:"):
                continue
            fragment = urlsplit(value).fragment
            if value.startswith("#"):
                assert fragment in page.ids, f"{path.name}: Anker {value} fehlt"
                continue
            target = _local_target(path, value)
            assert target.is_relative_to(WEBSITE.resolve()), f"{path.name}: {value}"
            assert target.is_file(), f"{path.name}: {value} fehlt"
            if fragment:
                assert fragment in _parse(target).ids, f"{path.name}: Anker {value} fehlt"


def test_tabs_control_existing_panels(pages):
    for path, page in pages.items():
        for panel_id in page.aria_controls:
            assert panel_id in page.ids, f"{path.name}: aria-controls={panel_id}"


# --- Datenschutz und Content-Security-Policy ------------------------------------


def test_pages_load_no_third_party_resources(pages):
    # Links nach außen sind erlaubt, nachgeladen wird aber nur von der eigenen Domain.
    for path, page in pages.items():
        for tag, attr, value, rel in page.refs:
            if tag == "a" or (tag == "link" and rel == "canonical"):
                continue
            assert not _is_external(value), f"{path.name}: <{tag} {attr}={value}>"


def test_pages_need_no_inline_script_or_style(pages):
    for path, page in pages.items():
        assert page.style_tags == 0, path.name
        assert page.style_attributes == [], path.name
        assert page.event_handlers == [], path.name
        for attributes, text in page.inline_scripts:
            # Strukturierte Daten werden nicht ausgeführt und fallen nicht unter script-src.
            assert attributes.get("type") == "application/ld+json", path.name
            json.loads(text)
        for _tag, _attr, value, _rel in page.refs:
            assert not value.lower().startswith("javascript:"), path.name


def test_stylesheet_imports_nothing():
    css = (WEBSITE / "assets" / "site.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert not re.search(r"url\(\s*['\"]?(?:https?:)?//", css)
    assert "@font-face" not in css


def test_sitemap_lists_existing_pages():
    sitemap = (WEBSITE / "sitemap.xml").read_text(encoding="utf-8")
    locations = re.findall(r"<loc>https://openbuchhaltung\.de/([^<]*)</loc>", sitemap)
    assert locations
    for location in locations:
        assert (WEBSITE / (location or "index.html")).is_file(), location
    robots = (WEBSITE / "robots.txt").read_text(encoding="utf-8")
    assert "Sitemap: https://openbuchhaltung.de/sitemap.xml" in robots


# --- Auslieferung über nginx ----------------------------------------------------


def test_website_overlay_mounts_directory_and_template():
    overlay = OVERLAY.read_text(encoding="utf-8")
    assert "./website:/usr/share/nginx/website:ro" in overlay
    assert (
        "./deploy/nginx/templates/website.conf.template:"
        "/etc/nginx/templates/website.conf.template:ro"
    ) in overlay
    # Ohne Webseiten-Overlay bindet der nginx nur seine eigene Vorlage ein.
    nginx_overlay = (PROJECT_ROOT / "docker-compose.nginx.yml").read_text(encoding="utf-8")
    assert "templates/website.conf.template:" not in nginx_overlay
    assert "./deploy/nginx/templates:/etc/nginx/templates" not in nginx_overlay


def test_website_template_variables_are_substituted():
    template = TEMPLATE.read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", template))
    assert used == {"WEBSITE_DOMAIN"}
    overlay = OVERLAY.read_text(encoding="utf-8")
    match = re.search(r"NGINX_ENVSUBST_FILTER: \^\(([^)]*)\)\$\$", overlay)
    assert match
    assert used <= set(match.group(1).split("|"))


def test_website_server_sends_strict_csp_and_keeps_no_access_log():
    template = TEMPLATE.read_text(encoding="utf-8")
    assert "root /usr/share/nginx/website;" in template
    # Zusage der Datenschutzerklärung: keine Zugriffsprotokolle.
    site_block = template.split("server_name ${WEBSITE_DOMAIN};", 1)[1]
    assert "access_log off;" in site_block
    csp = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', site_block)
    assert csp
    directives = dict(
        (part.split()[0], part.split()[1:]) for part in csp.group(1).split(";") if part.strip()
    )
    assert directives["default-src"] == ["'none'"]
    for directive in ("script-src", "style-src", "img-src"):
        assert directives[directive] == ["'self'"], directive
    assert "unsafe-inline" not in csp.group(1)
