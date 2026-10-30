"""Wissensbasis für Obi: Dateien, Suche, Kompaktprompt, Tool in API/MCP/UI."""

from __future__ import annotations

import json
from pathlib import Path

from app import create_app
from app.auth import hash_password
from app.services import knowledge
from app.services.chat import build_system_prompt, chat_tool_definitions
from app.services.knowledge import (
    KNOWLEDGE_DIR,
    KnowledgeBase,
    load_knowledge_base,
    parse_document,
    search_knowledge,
    tokenize,
)
from app.services.mcp_server import TOOLS, MCPServer
from domain.models import Company, Tenant, User

# Prompt + Tool-Definitionen: rund 50.000 Token bei ~3 Zeichen je Token (deutscher Text).
PROMPT_BUDGET_CHARS = 150_000


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None):
        from app.services.mcp_server import ApiResponse

        self.calls.append((method, path, params, json_body))
        return ApiResponse(
            status=200, text='{"ok": true}', content_type="application/json", json={"ok": True}
        )


def _create_test_app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'knowledge.db'}"}
    )
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        tenant = Tenant(name="Test")
        session.add(tenant)
        session.flush()
        session.add(Company(tenant_id=tenant.id, name="Test GmbH", currency_code="EUR"))
        session.commit()
    return app


def _logged_in_client(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return client


# ---------------------------------------------------------------------------
# Dateien und Parser
# ---------------------------------------------------------------------------


def test_bundled_knowledge_files_have_rechtsstand_and_sections():
    base = load_knowledge_base([KNOWLEDGE_DIR])
    assert len(base.documents) >= 8
    assert len(base.sections) >= 50
    for document in base.documents:
        assert document.rechtsstand, document.file
        assert document.sections, document.file
    ids = [section.id for section in base.sections]
    assert len(ids) == len(set(ids))
    assert base.rechtsstand() >= "2026-10"
    # Kompaktdateien sind nicht im Suchindex, aber im Prompttext.
    assert not any(section.file.startswith("kompakt") for section in base.sections)
    assert "Kleinunternehmer" in base.compact_text("skr03")
    assert "SKR03" in base.compact_text("skr03")
    assert "SKR04 (DATEV" in base.compact_text("skr04")
    assert "SKR04 (DATEV" not in base.compact_text("skr03")
    unknown = base.compact_text(None)
    assert "SKR03" in unknown and "SKR04 (DATEV" in unknown
    assert "Rechtsstand:" not in base.compact_text("skr03")


def test_parse_document_splits_sections_and_keywords():
    document = parse_document(
        "probe",
        "# Probe\nRechtsstand: 2026-01\nQuellen: UStG\n\n## Erster Teil\n"
        "Schlagworte: Alpha, Beta Gamma\nText eins.\n\n## Erster Teil\nText zwei.\n",
    )
    assert document.title == "Probe"
    assert document.rechtsstand == "2026-01"
    assert [section.id for section in document.sections] == [
        "probe/erster-teil",
        "probe/erster-teil-2",
    ]
    assert document.sections[0].keywords == ("Alpha", "Beta Gamma")
    assert document.sections[0].text == "Text eins."
    assert "Schlagworte" not in document.sections[0].text
    assert document.body.startswith("## Erster Teil")


def test_tokenize_folds_umlauts_and_stems():
    assert tokenize("Abschreibungen § 13b UStG") == ["abschreib", "13b", "ustg"]
    assert tokenize("Geringwertige Wirtschaftsgüter") == ["geringwertig", "wirtschaftsguet"]
    assert "der" not in tokenize("der Kleinunternehmer")


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def test_search_finds_expected_sections():
    base = load_knowledge_base([KNOWLEDGE_DIR])
    expectations = {
        "GWG Grenze": "anlagevermoegen-afa/geringwertige-wirtschaftsgueter",
        "Kleinunternehmer Umsatzgrenze": "umsatzsteuer/kleinunternehmer",
        "Bewirtung buchen": "betriebsausgaben/bewirtungskosten",
        "Minijob 2026": "lohn-sozialversicherung/mindestlohn-minijob",
        "UStVA Dauerfristverlängerung": "umsatzsteuer/umsatzsteuer-voranmeldung",
        "Abgabefrist Steuererklärung 2025": "ertragsteuern-fristen/steuererklaerungen-fristen",
        "Geldtransit": "buchungsmuster/bank-kasse-geldtransit",
        "Aufbewahrungsfrist Buchungsbelege": "buchfuehrung-gobd/aufbewahrungsfristen",
        "Vier-Augen-Prinzip Wertgrenze": "rechnungseingang-kontrolle/freigabe-vier-augen",
        "Prokura Handlungsvollmacht": "rechnungseingang-kontrolle/vertretungsmacht",
        "Kostenträger KOST2": "rechnungseingang-kontrolle/kostenstellen-kostentraeger",
        "Versandkosten aufteilen": "rechnungseingang-kontrolle/kontierung-aufteilung",
        "Kreditkartenabrechnung Vorsteuer": "rechnungseingang-kontrolle/firmenkreditkarten",
        "Dublette Eingangsrechnung": "rechnungseingang-kontrolle/rechnungseingang-ablauf",
        "Widerspruch Richtlinie": "rechnungseingang-kontrolle/unternehmensunterlagen",
    }
    for query, expected_prefix in expectations.items():
        hits = base.search(query)
        assert hits, query
        assert hits[0][0].id.startswith(expected_prefix), (query, [hit.id for hit, _ in hits])


def test_search_prefix_matches_compound_words():
    base = KnowledgeBase(
        [
            parse_document(
                "a",
                "# A\nRechtsstand: 2026-01\n\n## Regelung\nDie Kleinunternehmerregelung gilt.\n",
            ),
            parse_document(
                "b", "# B\nRechtsstand: 2026-01\n\n## Anderes\nBewirtung und Geschenke.\n"
            ),
        ]
    )
    hits = base.search("Kleinunternehmer")
    assert [hit.id for hit, _ in hits] == ["a/regelung"]
    assert base.search("") == []
    assert base.search("xyzzy") == []


def test_search_knowledge_returns_toc_without_query(monkeypatch):
    knowledge.reset_knowledge_base_cache()
    result = search_knowledge("")
    assert result["results"] == []
    assert result["toc"]
    assert result["rechtsstand"]
    hit = search_knowledge("Verpflegungspauschale", limit=2)
    assert len(hit["results"]) == 2
    assert hit["results"][0]["text"]
    assert hit["results"][0]["rechtsstand"]
    assert hit["toc"] == []


def test_extra_directory_is_indexed_and_compact_appended(tmp_path, monkeypatch):
    extra = tmp_path / "wissen-extra"
    extra.mkdir()
    (extra / "hausregeln.md").write_text(
        "# Hausregeln\nRechtsstand: 2026-11\n\n## Kaffeekasse\nSchlagworte: Zwergenkasse\n"
        "Barauslagen über die Zwergenkasse buchen.\n",
        encoding="utf-8",
    )
    (extra / "kompakt.md").write_text("# Extra\n\nImmer freundlich bleiben.\n", encoding="utf-8")
    monkeypatch.setenv(knowledge.EXTRA_DIR_ENV, str(extra))
    knowledge.reset_knowledge_base_cache()
    try:
        base = knowledge.get_knowledge_base()
        assert base.search("Zwergenkasse")[0][0].id == "hausregeln/kaffeekasse"
        assert base.rechtsstand() == "2026-11"
        compact = base.compact_text("skr03")
        assert "Immer freundlich bleiben." in compact
        # Allgemeine Kompakttexte (auch aus dem Zusatzverzeichnis) stehen vor der Kontenliste.
        assert compact.index("Immer freundlich bleiben.") < compact.index("SKR03 Kernkonten")
    finally:
        knowledge.reset_knowledge_base_cache()


# ---------------------------------------------------------------------------
# Systemprompt
# ---------------------------------------------------------------------------


def test_system_prompt_contains_compact_knowledge_within_budget():
    class _Company:
        name = "Test GmbH"
        id = 1
        currency_code = "EUR"

    tools_chars = len(json.dumps(chat_tool_definitions(), ensure_ascii=False))
    for chart in ("skr03", "skr04", None):
        prompt = build_system_prompt(_Company(), username="admin", role="Admin", chart=chart)
        assert "=== Fachwissen (Rechtsstand" in prompt
        assert "search_knowledge" in prompt
        assert len(prompt) + tools_chars <= PROMPT_BUDGET_CHARS, (chart, len(prompt), tools_chars)
    assert "bucht nach SKR03" in build_system_prompt(
        _Company(), username=None, role=None, chart="skr03"
    )
    assert "bucht nach SKR04" in build_system_prompt(
        _Company(), username=None, role=None, chart="skr04"
    )
    assert "nicht erkannt" in build_system_prompt(_Company(), username=None, role=None, chart=None)


# ---------------------------------------------------------------------------
# Tool in MCP, API und UI
# ---------------------------------------------------------------------------


def test_mcp_tool_search_knowledge_is_read_only_and_forwards_query():
    tool = next(tool for tool in TOOLS if tool.name == "search_knowledge")
    assert tool.http_method == "GET"
    assert tool.to_mcp()["title"] == "Fachwissen nachschlagen"
    definitions = {tool["name"]: tool for tool in chat_tool_definitions()}
    assert "Bestätigung" not in definitions["search_knowledge"]["description"]
    http = _RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "search_knowledge",
                "arguments": {"query": "GWG Grenze", "limit": 2},
            },
        }
    )
    assert http.calls[-1] == ("GET", "/knowledge", {"query": "GWG Grenze", "limit": 2}, None)


def test_api_and_ui_search_knowledge(tmp_path):
    knowledge.reset_knowledge_base_cache()
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    response = client.get(
        "/api/v1/knowledge", query_string={"q": "Kleinunternehmer Grenze", "limit": 1}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["rechtsstand"]
    assert len(payload["results"]) == 1
    assert payload["results"][0]["id"].startswith("umsatzsteuer/kleinunternehmer")
    assert "25.000" in payload["results"][0]["text"]

    toc = client.get("/api/v1/knowledge").get_json()
    assert toc["results"] == [] and toc["toc"]

    page = client.get("/wissen", query_string={"q": "Bewirtung"})
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Bewirtungskosten" in html
    assert "Rechtsstand" in html
    empty = client.get("/wissen").get_data(as_text=True)
    assert "Inhaltsverzeichnis" in empty
