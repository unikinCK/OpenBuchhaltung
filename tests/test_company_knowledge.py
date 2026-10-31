"""Firmenwissen je Gesellschaft (ADR-004): Paketimport, Status, Suche, API, MCP, Chat, UI.

Alle Inhalte sind erfunden (Muster GmbH); echte Unternehmensunterlagen gehören nie ins
Repository.
"""

from __future__ import annotations

import base64
import copy
import json
from io import BytesIO
from pathlib import Path

import pytest

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services import company_knowledge as ck
from app.services.chat import build_system_prompt, chat_tool_blocked
from app.services.company_knowledge import CompanyKnowledgeError
from app.services.mcp_server import MCPServer
from domain.models import (
    AuditLog,
    Company,
    CompanyKnowledgeEntry,
    CompanyKnowledgeExcerpt,
    CompanyKnowledgeSource,
    Tenant,
    User,
)

WRITER_TOKEN = "obk_firmenwissen-buchhalter"
READER_TOKEN = "obk_firmenwissen-pruefer"
OTHER_TOKEN = "obk_firmenwissen-fremd"

SHA_A = "a" * 64
SHA_B = "b" * 64


def _package(**overrides) -> dict:
    """Erfundenes Wissenspaket der Muster GmbH."""
    package = {
        "format": "openbuchhaltung-firmenwissen",
        "version": 1,
        "package": {"key": "muster-handbuch", "revision": "1", "title": "Musterhandbuch"},
        "sources": [
            {
                "key": "HB-1",
                "title": "Musterhandbuch Verwaltung",
                "origin": "handbuch/verwaltung.pdf",
                "sha256": SHA_A,
                "kind": "Richtlinie",
                "version": "2.0",
                "date": "2026-01-15",
                "valid_from": "2026-02-01",
                "authority": "binding",
            },
            {"key": "NOTIZ-1", "title": "Notiz Teamrunde", "kind": "Besprechung"},
        ],
        "excerpts": [
            {
                "key": "HB-1-S3",
                "source": "HB-1",
                "locator": "Seite 3",
                "content": "Büromaterial bestellt das Sekretariat gesammelt einmal im Monat.",
            },
            {
                "key": "HB-1-S4",
                "source": "HB-1",
                "locator": "Seite 4",
                "content": "Getränke für Besprechungen gehen auf die Kostenstelle Verwaltung.",
            },
            {
                "key": "NOTIZ-1-A",
                "source": "NOTIZ-1",
                "locator": "Absatz 1",
                "content": "Idee: Zeitschriften künftig digital beziehen.",
            },
        ],
        "entries": [
            {
                "key": "MW-001",
                "topic": "Beschaffung",
                "title": "Büromaterial monatlich bündeln",
                "content": "Büromaterial bestellt das Sekretariat gesammelt einmal im Monat.",
                "source": "HB-1",
                "locator": "Seite 3",
                "review_due": "2027-01-31",
                "review_note": "Mit dem Sekretariat abstimmen.",
            },
            {
                "key": "MW-002",
                "topic": "Kontierung",
                "title": "Besprechungsgetränke",
                "content": "Getränke für Besprechungen gehen auf die Kostenstelle Verwaltung.",
                "source": "HB-1",
                "locator": "Seite 4",
            },
            {
                "key": "MW-Q1",
                "kind": "open_question",
                "topic": "Abonnements",
                "title": "Zeitschriften digital?",
                "content": "Unklar, ob Zeitschriften künftig digital bezogen werden.",
                "source": "NOTIZ-1",
            },
        ],
    }
    package.update(overrides)
    return package


def _create_test_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'firmenwissen.db'}",
            "API_REQUIRE_AUTH": True,
        }
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
        tenant = Tenant(name="Muster")
        other_tenant = Tenant(name="Fremd")
        session.add_all([tenant, other_tenant])
        session.flush()
        session.add(Company(tenant_id=tenant.id, name="Muster GmbH", currency_code="EUR"))
        session.add(Company(tenant_id=other_tenant.id, name="Fremd AG", currency_code="EUR"))
        for username, role, token, tenant_id in (
            ("buchhalter", "Buchhalter", WRITER_TOKEN, tenant.id),
            ("pruefer", "Pruefer", READER_TOKEN, tenant.id),
            ("fremd", "Buchhalter", OTHER_TOKEN, other_tenant.id),
        ):
            session.add(
                User(
                    username=username,
                    password_hash=hash_password("passwort123"),
                    role=role,
                    tenant_id=tenant_id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        session.commit()
    ck.invalidate_company_index()
    return app


def _session(app):
    return app.extensions["db_session_factory"]()


def _import(app, package: dict, *, dry_run: bool = False) -> dict:
    with _session(app) as session:
        company = session.get(Company, 1)
        report = ck.import_package(
            session=session, company=company, data=package, changed_by="tester", dry_run=dry_run
        )
        if dry_run:
            session.rollback()
        else:
            session.commit()
        return report


def _entry(session, key: str) -> CompanyKnowledgeEntry:
    return session.query(CompanyKnowledgeEntry).filter_by(company_id=1, entry_key=key).one()


def _approve(app, key: str, user: str = "chefin") -> None:
    with _session(app) as session:
        ck.set_entry_status(
            session=session, entry=_entry(session, key), status="approved", changed_by=user
        )
        session.commit()


def _logged_in_client(app, username="admin", password="admin123"):
    client = app.test_client()
    client.post("/auth/login", data={"username": username, "password": password})
    return client


# ---------------------------------------------------------------------------
# Paket prüfen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update(format="anderes"), "format"),
        (lambda p: p.update(version=2), "version"),
        (lambda p: p.update(package={"key": "mit leerzeichen"}), "package.key"),
        (lambda p: p["sources"].append(dict(p["sources"][0])), "doppelt"),
        (lambda p: p["excerpts"][0].update(source="FEHLT"), "fehlt im Paket"),
        (lambda p: p["entries"][0].update(source="FEHLT"), "fehlt im Paket"),
        (lambda p: p["entries"][0].update(review_due="31.01.2027"), "JJJJ-MM-TT"),
        (lambda p: p["sources"][0].update(sha256="xyz"), "sha256"),
        (lambda p: p["sources"][0].update(authority="wichtig"), "authority"),
        (lambda p: p["entries"][1].pop("content"), "content"),
        (lambda p: p["entries"][2].update(kind="regel"), "kind"),
    ],
)
def test_parse_package_rejects_invalid_packages(mutate, message):
    package = _package()
    mutate(package)
    with pytest.raises(CompanyKnowledgeError, match=message):
        ck.parse_package(package)


def test_parse_package_accepts_minimal_package():
    parsed = ck.parse_package(
        {"format": "openbuchhaltung-firmenwissen", "version": 1, "package": {"key": "leer"}}
    )
    assert parsed.key == "leer" and parsed.sources == [] and parsed.entries == []


# ---------------------------------------------------------------------------
# Import und Abgleich
# ---------------------------------------------------------------------------


def test_import_creates_drafts_and_dry_run_changes_nothing(tmp_path):
    app = _create_test_app(tmp_path)
    preview = _import(app, _package(), dry_run=True)
    with _session(app) as session:
        assert session.query(CompanyKnowledgeEntry).count() == 0
        assert session.query(CompanyKnowledgeSource).count() == 0
        knowledge_events = AuditLog.entity_type.like("company_knowledge%")
        assert session.query(AuditLog).filter(knowledge_events).count() == 0

    report = _import(app, _package())
    for part in ("sources", "excerpts", "entries"):
        assert report[part] == {**preview[part]}
    assert report["sources"]["created"] == 2
    assert report["excerpts"]["created"] == 3
    assert report["entries"]["created"] == 3
    assert report["dry_run"] is False and preview["dry_run"] is True

    with _session(app) as session:
        entries = session.query(CompanyKnowledgeEntry).all()
        assert {entry.status for entry in entries} == {"draft"}
        assert {entry.proposed_by for entry in entries} == {"tester"}
        first = _entry(session, "MW-001")
        assert first.source.source_key == "HB-1" and first.locator == "Seite 3"
        assert first.review_due.isoformat() == "2027-01-31"
        assert _entry(session, "MW-Q1").kind == "open_question"
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="HB-1").one()
        assert source.authority == "binding" and source.valid_from.isoformat() == "2026-02-01"
        assert len(source.excerpts) == 2
        events = session.query(AuditLog).filter(AuditLog.company_id == 1).all()
        assert sum(event.entity_type == "company_knowledge_entry" for event in events) == 3
        package_events = [e for e in events if e.entity_type == "company_knowledge_package"]
        assert len(package_events) == 1 and package_events[0].entity_id == "muster-handbuch"

    again = _import(app, _package())
    assert again["entries"]["unchanged"] == 3 and again["entries"]["updated"] == 0
    assert again["sources"]["unchanged"] == 2 and again["excerpts"]["unchanged"] == 3


def test_reimport_resets_changed_approved_entries_and_flags_changed_sources(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    _approve(app, "MW-001")
    _approve(app, "MW-002")

    changed = _package(package={"key": "muster-handbuch", "revision": "2"})
    changed["entries"][0]["content"] = "Büromaterial bestellt das Sekretariat jede Woche."
    changed["sources"][0]["sha256"] = SHA_B
    preview = _import(app, copy.deepcopy(changed), dry_run=True)
    report = _import(app, changed)
    assert preview["entries"] == report["entries"]
    assert report["entries"]["back_to_draft"] == 1
    assert report["entries"]["flagged"] == 1

    with _session(app) as session:
        first = _entry(session, "MW-001")
        assert first.status == "draft" and first.approved_by is None
        assert first.review_reason == ck.REASON_CONTENT_CHANGED
        assert "jede Woche" in first.content
        second = _entry(session, "MW-002")
        assert second.status == "approved" and second.review_reason == ck.REASON_SOURCE_CHANGED
        assert ck.entry_needs_review(second)

    # Erneute Freigabe bestätigt den geprüften Eintrag und löscht den Prüfgrund.
    _approve(app, "MW-002", user="pruefende")
    with _session(app) as session:
        second = _entry(session, "MW-002")
        assert second.review_reason is None and second.approved_by == "pruefende"


def test_reimport_retires_removed_entries_and_revives_them_later(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    _approve(app, "MW-002")

    reduced = _package()
    reduced["entries"] = [entry for entry in reduced["entries"] if entry["key"] != "MW-Q1"]
    reduced["sources"] = [source for source in reduced["sources"] if source["key"] != "NOTIZ-1"]
    reduced["excerpts"] = [item for item in reduced["excerpts"] if item["source"] != "NOTIZ-1"]
    report = _import(app, reduced)
    assert report["entries"]["removed"] == 1
    assert report["sources"]["removed"] == 1 and report["excerpts"]["removed"] == 1

    with _session(app) as session:
        question = _entry(session, "MW-Q1")
        assert question.status == "retired" and question.review_reason == ck.REASON_ENTRY_GONE
        notiz = session.query(CompanyKnowledgeSource).filter_by(source_key="NOTIZ-1").one()
        assert notiz.is_active is False and notiz.inactive_reason == ck.REASON_ENTRY_GONE
        assert _entry(session, "MW-002").status == "approved"

    revived = _import(app, _package())
    assert revived["entries"]["revived"] == 1
    with _session(app) as session:
        question = _entry(session, "MW-Q1")
        assert question.status == "draft" and question.review_reason is None
        notiz = session.query(CompanyKnowledgeSource).filter_by(source_key="NOTIZ-1").one()
        assert notiz.is_active is True and notiz.inactive_reason is None


def test_manual_decisions_survive_reimport(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    with _session(app) as session:
        ck.set_entry_status(
            session=session, entry=_entry(session, "MW-Q1"), status="retired", changed_by="chefin"
        )
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="NOTIZ-1").one()
        ck.set_source_active(session=session, source=source, is_active=False, changed_by="chefin")
        session.commit()

    _import(app, _package())
    with _session(app) as session:
        assert _entry(session, "MW-Q1").status == "retired"
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="NOTIZ-1").one()
        assert source.is_active is False and source.inactive_reason == ck.REASON_SOURCE_MANUAL


def test_import_refuses_keys_of_manual_entries_or_other_packages(tmp_path):
    app = _create_test_app(tmp_path)
    with _session(app) as session:
        ck.create_entry(
            session=session,
            company=session.get(Company, 1),
            title="Handnotiz",
            content="Von Hand angelegt.",
            entry_key="MW-001",
            changed_by="chefin",
        )
        session.commit()
    with pytest.raises(CompanyKnowledgeError, match="manuell angelegt"):
        _import(app, _package())

    other = _package(package={"key": "anderes-paket"})
    other["entries"] = []
    _import(app, other)
    with pytest.raises(CompanyKnowledgeError, match="Paket „anderes-paket“"):
        _import(app, _package(entries=[]))


def test_approved_entry_from_removed_source_of_other_package_is_flagged(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    with _session(app) as session:
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="HB-1").one()
        manual = ck.create_entry(
            session=session,
            company=session.get(Company, 1),
            title="Eigene Ergänzung",
            content="Ergänzung zur Seite 4.",
            source_id=source.id,
            changed_by="chefin",
        )
        ck.set_entry_status(session=session, entry=manual, status="approved", changed_by="chefin")
        session.commit()
        manual_id = manual.id

    without_handbook = _package(sources=[_package()["sources"][1]], excerpts=[], entries=[])
    report = _import(app, without_handbook)
    assert report["entries"]["flagged"] == 1
    with _session(app) as session:
        manual = session.get(CompanyKnowledgeEntry, manual_id)
        assert manual.status == "approved" and manual.review_reason == ck.REASON_SOURCE_GONE


# ---------------------------------------------------------------------------
# Einträge ändern, Status
# ---------------------------------------------------------------------------


def test_update_resets_approval_only_for_substantive_changes(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    _approve(app, "MW-002")
    with _session(app) as session:
        entry = _entry(session, "MW-002")
        from datetime import date

        assert ck.update_entry(
            session=session, entry=entry, changed_by="chefin", review_due=date(2027, 6, 30)
        )
        assert entry.status == "approved"
        new_content = "Getränke: Kostenstelle Empfang."
        assert ck.update_entry(
            session=session, entry=entry, changed_by="chefin", content=new_content
        )
        assert entry.status == "draft" and entry.approved_by is None
        assert not ck.update_entry(
            session=session, entry=entry, changed_by="chefin", content=new_content
        )
        assert not ck.update_entry(
            session=session, entry=entry, changed_by="chefin", review_note=entry.review_note
        )
        with pytest.raises(CompanyKnowledgeError, match="Angabe fehlt"):
            ck.update_entry(session=session, entry=entry, changed_by="chefin", title="  ")
        ck.set_entry_status(session=session, entry=entry, status="retired", changed_by="chefin")
        with pytest.raises(CompanyKnowledgeError, match="Entwurf aktivieren"):
            ck.set_entry_status(session=session, entry=entry, status="approved", changed_by="x")
        with pytest.raises(CompanyKnowledgeError, match="Entwurf aktivieren"):
            ck.update_entry(session=session, entry=entry, changed_by="chefin", title="Neu")
        ck.set_entry_status(session=session, entry=entry, status="draft", changed_by="chefin")
        assert entry.status == "draft"
        session.commit()
        actions = [
            event.action
            for event in ck.knowledge_history(
                session=session,
                company_id=1,
                entity_type="company_knowledge_entry",
                entity_id=entry.id,
            )
        ]
    assert actions[:4] == ["set_draft", "retired", "updated", "updated"]
    assert "approved" in actions


def test_deactivating_a_source_flags_approved_entries(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    _approve(app, "MW-001")
    with _session(app) as session:
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="HB-1").one()
        ck.set_source_active(session=session, source=source, is_active=False, changed_by="chefin")
        session.commit()
        assert _entry(session, "MW-001").review_reason == ck.REASON_SOURCE_DEACTIVATED
        summary = ck.company_knowledge_summary(session=session, company_id=1)
    assert summary == {"approved": 1, "drafts": 2, "retired": 0, "due": 3, "sources": 1}


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


def test_company_search_labels_status_and_skips_retired_and_inactive(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    with _session(app) as session:
        hits = ck.search_company_knowledge(
            session=session, company_id=1, query="Büromaterial Sekretariat", limit=5
        )
        assert hits[0]["type"] in {"entry", "excerpt"}
        entry_hit = next(hit for hit in hits if hit["type"] == "entry")
        assert entry_hit["status"] == "draft"
        assert entry_hit["status_label"] == "Entwurf – nicht freigegeben"
        assert entry_hit["source_title"] == "Musterhandbuch Verwaltung"
        assert entry_hit["locator"] == "Seite 3"
        excerpt_hit = next(hit for hit in hits if hit["type"] == "excerpt")
        assert "maßgeblich" in excerpt_hit["status_label"]

    _approve(app, "MW-001")
    with _session(app) as session:
        hits = ck.search_company_knowledge(
            session=session, company_id=1, query="Büromaterial Sekretariat", limit=5
        )
        # Freigegebener Eintrag schlägt den gleichlautenden Quellenauszug.
        assert hits[0]["type"] == "entry" and hits[0]["status"] == "approved"

        ck.set_entry_status(
            session=session, entry=_entry(session, "MW-Q1"), status="retired", changed_by="x"
        )
        source = session.query(CompanyKnowledgeSource).filter_by(source_key="NOTIZ-1").one()
        ck.set_source_active(session=session, source=source, is_active=False, changed_by="x")
        session.commit()
        assert ck.search_company_knowledge(
            session=session, company_id=1, query="Zeitschriften digital", limit=5
        ) == []
        # Andere Gesellschaft sieht nichts.
        assert ck.search_company_knowledge(
            session=session, company_id=2, query="Büromaterial", limit=5
        ) == []


# ---------------------------------------------------------------------------
# REST-API
# ---------------------------------------------------------------------------


def test_api_entries_sources_import_and_roles(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()
    writer = {"Authorization": f"Bearer {WRITER_TOKEN}"}
    reader = {"Authorization": f"Bearer {READER_TOKEN}"}
    other = {"Authorization": f"Bearer {OTHER_TOKEN}"}
    base = "/api/v1/companies/1/knowledge"

    package_body = {"package": _package()}
    assert client.post(f"{base}/import", headers=reader, json=package_body).status_code == 403
    assert client.post(f"{base}/import", headers=other, json=package_body).status_code == 404

    preview = client.post(
        f"{base}/import", headers=writer, json={"package": _package(), "dry_run": True}
    )
    assert preview.status_code == 200 and preview.get_json()["dry_run"] is True
    assert client.get(f"{base}/entries", headers=reader).get_json()["total"] == 0

    encoded = base64.b64encode(json.dumps(_package()).encode()).decode()
    imported = client.post(f"{base}/import", headers=writer, json={"content_base64": encoded})
    assert imported.status_code == 201
    assert imported.get_json()["entries"]["created"] == 3
    broken = client.post(f"{base}/import", headers=writer, json={"content_base64": "kein-base64!"})
    assert broken.status_code == 422

    listing = client.get(f"{base}/entries", headers=reader, query_string={"due": "true"})
    assert listing.status_code == 200
    payload = listing.get_json()
    assert payload["total"] == 3 and "content" not in payload["entries"][0]
    assert client.get(f"{base}/entries", headers=other).status_code == 404

    entry_id = next(e["id"] for e in payload["entries"] if e["entry_key"] == "MW-002")
    detail = client.get(f"/api/v1/knowledge-entries/{entry_id}", headers=reader).get_json()
    assert detail["content"].startswith("Getränke") and detail["status"] == "draft"
    assert client.get(f"/api/v1/knowledge-entries/{entry_id}", headers=other).status_code == 404

    assert client.post(
        f"/api/v1/knowledge-entries/{entry_id}/status", headers=reader, json={"status": "approved"}
    ).status_code == 403
    approved = client.post(
        f"/api/v1/knowledge-entries/{entry_id}/status", headers=writer, json={"status": "approved"}
    )
    assert approved.status_code == 200 and approved.get_json()["approved_by"] == "buchhalter"
    bad_status = client.post(
        f"/api/v1/knowledge-entries/{entry_id}/status", headers=writer, json={"status": "fertig"}
    )
    assert bad_status.status_code == 422
    no_status = client.post(f"/api/v1/knowledge-entries/{entry_id}/status", headers=writer, json={})
    assert no_status.status_code == 422

    patched = client.patch(
        f"/api/v1/knowledge-entries/{entry_id}", headers=writer, json={"topic": "Bewirtung intern"}
    )
    assert patched.status_code == 200
    assert patched.get_json()["status"] == "approved" and patched.get_json()["changed"] is True
    assert client.patch(
        f"/api/v1/knowledge-entries/{entry_id}", headers=writer, json={"status": "approved"}
    ).status_code == 400

    created = client.post(
        f"{base}/entries",
        headers=writer,
        json={"title": "Neue Regel", "content": "Post öffnet die Zentrale.", "topic": "Post"},
    )
    assert created.status_code == 201 and created.get_json()["status"] == "draft"
    assert created.get_json()["entry_key"].startswith("manuell-")
    incomplete = client.post(f"{base}/entries", headers=writer, json={"title": "ohne Inhalt"})
    assert incomplete.status_code == 422

    sources = client.get(f"{base}/sources", headers=reader).get_json()["sources"]
    handbook = next(source for source in sources if source["source_key"] == "HB-1")
    assert handbook["excerpt_count"] == 2 and handbook["entry_count"] == 2
    source_url = f"/api/v1/knowledge-sources/{handbook['id']}"
    source_detail = client.get(source_url, headers=reader).get_json()
    assert [item["locator"] for item in source_detail["excerpts"]] == ["Seite 3", "Seite 4"]
    toggled = client.post(f"{source_url}/active", headers=writer, json={"is_active": False})
    assert toggled.status_code == 200 and toggled.get_json()["is_active"] is False

    search = client.get(
        "/api/v1/knowledge", headers=reader, query_string={"q": "Post Zentrale", "company_id": 1}
    ).get_json()
    assert search["company"]["drafts"] >= 1 and "nicht freigegeben" in search["company"]["note"]
    assert search["company_results"][0]["title"] == "Neue Regel"
    assert client.get(
        "/api/v1/knowledge", headers=other, query_string={"q": "Post", "company_id": 1}
    ).status_code == 404
    plain = client.get("/api/v1/knowledge", headers=reader, query_string={"q": "Post"}).get_json()
    assert "company_results" not in plain


# ---------------------------------------------------------------------------
# MCP und Chat
# ---------------------------------------------------------------------------


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None):
        from app.services.mcp_server import ApiResponse

        self.calls.append((method, path, params, json_body))
        return ApiResponse(
            status=200, text='{"ok": true}', content_type="application/json", json={"ok": True}
        )


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        (
            "list_company_knowledge_entries",
            {"company_id": 3, "due": True},
            ("GET", "/companies/3/knowledge/entries", {"due": True}, None),
        ),
        (
            "get_company_knowledge_entry",
            {"entry_id": 7},
            ("GET", "/knowledge-entries/7", None, None),
        ),
        (
            "create_company_knowledge_entry",
            {"company_id": 3, "title": "T", "content": "C"},
            ("POST", "/companies/3/knowledge/entries", None, {"title": "T", "content": "C"}),
        ),
        (
            "update_company_knowledge_entry",
            {"entry_id": 7, "topic": None},
            ("PATCH", "/knowledge-entries/7", None, {"topic": None}),
        ),
        (
            "set_company_knowledge_entry_status",
            {"entry_id": 7, "status": "approved"},
            ("POST", "/knowledge-entries/7/status", None, {"status": "approved"}),
        ),
        (
            "import_company_knowledge",
            {"company_id": 3, "package": {"format": "x"}, "dry_run": True},
            (
                "POST",
                "/companies/3/knowledge/import",
                None,
                {"package": {"format": "x"}, "dry_run": True},
            ),
        ),
        (
            "search_knowledge",
            {"query": "Beschaffung", "company_id": 3},
            ("GET", "/knowledge", {"query": "Beschaffung", "company_id": 3}, None),
        ),
    ],
)
def test_mcp_tools_proxy_to_api(name, arguments, expected):
    http = _RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    method, path, params, body = http.calls[-1]
    assert (method, path) == expected[:2]
    if expected[2] is not None:
        assert params == expected[2]
    if expected[3] is not None:
        assert body == expected[3]


def test_chat_blocks_approval_and_import_but_offers_reading_and_proposing():
    for name in (
        "set_company_knowledge_entry_status",
        "set_company_knowledge_source_active",
        "import_company_knowledge",
    ):
        assert chat_tool_blocked(name)
    for name in (
        "list_company_knowledge_entries",
        "get_company_knowledge_entry",
        "create_company_knowledge_entry",
        "update_company_knowledge_entry",
        "list_company_knowledge_sources",
        "get_company_knowledge_source",
    ):
        assert not chat_tool_blocked(name)


def test_system_prompt_mentions_company_knowledge_only_when_present():
    class _Company:
        name = "Muster GmbH"
        id = 4
        currency_code = "EUR"

    empty = {"approved": 0, "drafts": 0, "retired": 2, "due": 0, "sources": 1}
    assert "=== Firmenwissen ===" not in build_system_prompt(
        _Company(), username="a", role="Admin", chart="skr03", company_knowledge=empty
    )
    prompt = build_system_prompt(
        _Company(),
        username="a",
        role="Admin",
        chart="skr03",
        company_knowledge={"approved": 5, "drafts": 2, "retired": 0, "due": 2, "sources": 3},
    )
    assert "=== Firmenwissen ===" in prompt
    assert "5 freigegebene Einträge, 2 Entwürfe" in prompt
    assert "company_id=4" in prompt and "nicht freigegeben" in prompt


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------


def test_ui_import_review_approve_and_search(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    page = client.get("/firmenwissen", query_string={"company_id": 1})
    assert page.status_code == 200 and "Firmenwissen" in page.get_data(as_text=True)

    upload = client.post(
        "/firmenwissen/import",
        data={
            "company_id": "1",
            "dry_run": "1",
            "package_file": (BytesIO(json.dumps(_package()).encode()), "paket.json"),
        },
        content_type="multipart/form-data",
    )
    html = upload.get_data(as_text=True)
    assert upload.status_code == 200 and "Vorschau (nichts geändert)" in html
    with _session(app) as session:
        assert session.query(CompanyKnowledgeEntry).count() == 0

    applied = client.post(
        "/firmenwissen/import",
        data={
            "company_id": "1",
            "package_file": (BytesIO(json.dumps(_package()).encode()), "paket.json"),
        },
        content_type="multipart/form-data",
    )
    assert "Import abgeschlossen" in applied.get_data(as_text=True)

    listing = client.get("/firmenwissen", query_string={"company_id": 1, "due": "1"})
    assert "Büromaterial monatlich bündeln" in listing.get_data(as_text=True)
    sources_view = client.get("/firmenwissen", query_string={"company_id": 1, "view": "sources"})
    assert "Musterhandbuch Verwaltung" in sources_view.get_data(as_text=True)

    with _session(app) as session:
        entry_id = _entry(session, "MW-001").id
        source_id = session.query(CompanyKnowledgeSource).filter_by(source_key="HB-1").one().id
    detail = client.get(f"/firmenwissen/eintraege/{entry_id}").get_data(as_text=True)
    assert "Entwurf – nicht freigegeben" in detail and "Seite 3" in detail
    client.post(f"/firmenwissen/eintraege/{entry_id}/status", data={"status": "approved"})
    detail = client.get(f"/firmenwissen/eintraege/{entry_id}").get_data(as_text=True)
    assert "freigegeben von admin" in detail

    client.post(
        f"/firmenwissen/eintraege/{entry_id}/aendern",
        data={
            "title": "Büromaterial monatlich bündeln",
            "content": "Büromaterial bestellt das Sekretariat gesammelt zum Monatsende.",
            "kind": "knowledge",
            "topic": "Beschaffung",
            "source_id": str(source_id),
            "locator": "Seite 3",
        },
    )
    with _session(app) as session:
        assert _entry(session, "MW-001").status == "draft"

    source_page = client.get(f"/firmenwissen/quellen/{source_id}").get_data(as_text=True)
    assert "Seite 4" in source_page and "maßgeblich" in source_page

    created = client.post(
        "/firmenwissen/eintraege",
        data={"company_id": "1", "title": "Posteingang", "content": "Post öffnet die Zentrale."},
        follow_redirects=True,
    )
    assert "Posteingang" in created.get_data(as_text=True)

    wissen = client.get("/wissen", query_string={"q": "Sekretariat Monatsende", "company_id": 1})
    html = wissen.get_data(as_text=True)
    assert "Firmenwissen der Gesellschaft" in html and "Büromaterial monatlich bündeln" in html


def test_ui_reader_sees_no_write_forms(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    client = _logged_in_client(app, "pruefer", "passwort123")
    page = client.get("/firmenwissen", query_string={"company_id": 1}).get_data(as_text=True)
    assert "Import" not in page.split("Ansicht")[1].split("</nav>")[0]
    with _session(app) as session:
        entry_id = _entry(session, "MW-001").id
    detail = client.get(f"/firmenwissen/eintraege/{entry_id}").get_data(as_text=True)
    assert "Freigeben" not in detail and "Bearbeiten" not in detail
    blocked = client.post(f"/firmenwissen/eintraege/{entry_id}/status", data={"status": "approved"})
    assert blocked.status_code == 302
    with _session(app) as session:
        assert _entry(session, "MW-001").status == "draft"
    # Fremder Mandant: Seite der Gesellschaft nicht erreichbar.
    stranger = _logged_in_client(app, "fremd", "passwort123")
    assert stranger.get(f"/firmenwissen/eintraege/{entry_id}").status_code == 404


def test_excerpts_unique_per_company(tmp_path):
    app = _create_test_app(tmp_path)
    _import(app, _package())
    with _session(app) as session:
        assert session.query(CompanyKnowledgeExcerpt).filter_by(company_id=1).count() == 3
