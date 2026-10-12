"""Belegkategorien und mehrere Belege je Buchung (UI, API, Migration)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from alembic import command
from document_files import pdf_document_bytes
from sqlalchemy import create_engine, text

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config
from app.services.document_categories import DEFAULT_DOCUMENT_CATEGORIES
from domain.models import AuditLog, Company, Document, DocumentCategory, User


def _create_test_app(tmp_path: Path, **extra_config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'categories.db'}",
            **extra_config,
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
        session.commit()
    return app


def _logged_in_client(app, username: str = "admin", password: str = "admin123"):
    client = app.test_client()
    client.post("/auth/login", data={"username": username, "password": password})
    return client


def _setup_company_with_entry(client) -> None:
    """Mandant + Gesellschaft (ID 1) über die UI, zwei Konten, eine Buchung (ID 1)."""
    client.post(
        "/tenants",
        data={"tenant_name": "Beleg-Mandant", "company_name": "Beleg GmbH"},
        follow_redirects=True,
    )
    for code, name, account_type in (("1200", "Bank", "asset"), ("4900", "Aufwand", "expense")):
        client.post(
            "/accounts",
            data={"company_id": "1", "code": code, "name": name, "account_type": account_type},
            follow_redirects=True,
        )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Bewirtung Kunde",
            "debit_account_id": "2",
            "credit_account_id": "1",
            "amount": "119.00",
        },
        follow_redirects=True,
    )


def _category_id(app, name: str) -> int:
    with app.extensions["db_session_factory"]() as session:
        return (
            session.query(DocumentCategory)
            .filter(DocumentCategory.company_id == 1, DocumentCategory.name == name)
            .one()
            .id
        )


def _upload_ui(client, marker: bytes, file_name: str, category_id: int | None = None):
    data = {
        "company_id": "1",
        "journal_entry_id": "1",
        "document_date": "2026-04-04",
        "document_file": (BytesIO(pdf_document_bytes(marker)), file_name),
    }
    if category_id is not None:
        data["category_id"] = str(category_id)
    return client.post(
        "/documents", data=data, content_type="multipart/form-data", follow_redirects=True
    )


def test_new_company_gets_default_categories(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    _setup_company_with_entry(client)

    with app.extensions["db_session_factory"]() as session:
        categories = (
            session.query(DocumentCategory)
            .filter(DocumentCategory.company_id == 1)
            .order_by(DocumentCategory.sort_order)
            .all()
        )
        assert [category.name for category in categories] == list(DEFAULT_DOCUMENT_CATEGORIES)
        assert all(category.is_default and category.is_active for category in categories)

    page = client.get("/belege?company_id=1")
    assert page.status_code == 200
    for name in DEFAULT_DOCUMENT_CATEGORIES:
        assert name.encode() in page.data


def test_api_tenant_creation_seeds_categories(tmp_path):
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=False)
    client = app.test_client()
    created = client.post(
        "/api/v1/tenants",
        json={"tenant_name": "API-Mandant", "company_name": "API GmbH"},
    )
    assert created.status_code == 201
    company_id = created.get_json()["company"]["id"]

    listed = client.get(f"/api/v1/document-categories?company_id={company_id}")
    assert listed.status_code == 200
    assert [c["name"] for c in listed.get_json()["categories"]] == list(
        DEFAULT_DOCUMENT_CATEGORIES
    )


def test_multiple_documents_per_entry_with_categories_via_ui(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    _setup_company_with_entry(client)
    invoice_id = _category_id(app, "Bewirtungsbeleg")
    payment_id = _category_id(app, "Zahlungsbeleg")

    first = _upload_ui(client, b"bewirtung", "bewirtung.pdf", invoice_id)
    assert b"Beleg wurde hochgeladen" in first.data
    assert b"Beleg wurde hochgeladen" in _upload_ui(client, b"zahlung", "ec-karte.pdf").data

    with app.extensions["db_session_factory"]() as session:
        documents = session.query(Document).order_by(Document.id).all()
        assert [document.journal_entry_id for document in documents] == [1, 1]
        assert documents[0].category_id == invoice_id
        assert documents[1].category_id is None
        second_id = documents[1].id

    categorized = client.post(
        f"/documents/{second_id}/category",
        data={"category_id": str(payment_id)},
        follow_redirects=True,
    )
    assert b"Belegkategorie wurde gespeichert" in categorized.data

    with app.extensions["db_session_factory"]() as session:
        assert session.get(Document, second_id).category_id == payment_id
        audit = (
            session.query(AuditLog)
            .filter(AuditLog.entity_type == "document", AuditLog.action == "categorized")
            .one()
        )
        assert audit.payload["category_name"] == "Zahlungsbeleg"

    # Buchungsliste zeigt beide Belege mit Kategorie an der Buchung.
    journal = client.get("/buchungen?company_id=1")
    assert journal.status_code == 200
    assert b"Bewirtungsbeleg: bewirtung.pdf" in journal.data
    assert b"Zahlungsbeleg: ec-karte.pdf" in journal.data
    assert b"+ weiterer Beleg" in journal.data

    # „+ weiterer Beleg“ wählt die Buchung im Uploadformular vor.
    preselected = client.get("/belege?company_id=1&journal_entry_id=1")
    assert b'<option value="1" selected>' in preselected.data


def test_categories_can_be_extended_and_deactivated_via_ui(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    _setup_company_with_entry(client)

    created = client.post(
        "/belegkategorien",
        data={"company_id": "1", "name": "  Lieferschein "},
        follow_redirects=True,
    )
    assert "Belegkategorie „Lieferschein“ wurde angelegt".encode() in created.data

    duplicate = client.post(
        "/belegkategorien",
        data={"company_id": "1", "name": "lieferschein"},
        follow_redirects=True,
    )
    assert "existiert bereits".encode() in duplicate.data

    category_id = _category_id(app, "Lieferschein")
    with app.extensions["db_session_factory"]() as session:
        category = session.get(DocumentCategory, category_id)
        assert category.is_default is False
        assert category.sort_order > 50

    client.post(
        f"/belegkategorien/{category_id}/aktiv",
        data={"is_active": "0"},
        follow_redirects=True,
    )
    rejected = _upload_ui(client, b"lieferschein", "lieferschein.pdf", category_id)
    assert "ist deaktiviert".encode() in rejected.data
    with app.extensions["db_session_factory"]() as session:
        assert session.get(DocumentCategory, category_id).is_active is False
        assert session.query(Document).count() == 0


def test_read_only_role_cannot_extend_categories(tmp_path):
    app = _create_test_app(tmp_path)
    admin = _logged_in_client(app)
    _setup_company_with_entry(admin)
    with app.extensions["db_session_factory"]() as session:
        tenant_id = session.get(Company, 1).tenant_id
        session.add(
            User(
                username="pruefer",
                password_hash=hash_password("pruefer123"),
                role="Pruefer",
                tenant_id=tenant_id,
            )
        )
        session.commit()

    pruefer = _logged_in_client(app, "pruefer", "pruefer123")
    page = pruefer.get("/belege?company_id=1")
    assert page.status_code == 200
    assert b"Kategorie anlegen" not in page.data

    response = pruefer.post(
        "/belegkategorien", data={"company_id": "1", "name": "Quittung"}, follow_redirects=True
    )
    assert "Ihre Rolle erlaubt nur Lesezugriff".encode() in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(DocumentCategory).filter_by(name="Quittung").count() == 0


def test_document_categories_via_api(tmp_path):
    buchhalter_token = "obk_buchhalter-kategorie"
    pruefer_token = "obk_pruefer-kategorie"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    admin = _logged_in_client(app)
    _setup_company_with_entry(admin)
    with app.extensions["db_session_factory"]() as session:
        tenant_id = session.get(Company, 1).tenant_id
        for username, role, token in (
            ("api-buchhalter", "Buchhalter", buchhalter_token),
            ("api-pruefer", "Pruefer", pruefer_token),
        ):
            session.add(
                User(
                    username=username,
                    password_hash=hash_password("passwort"),
                    role=role,
                    tenant_id=tenant_id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        session.commit()

    client = app.test_client()
    writer = {"Authorization": f"Bearer {buchhalter_token}"}
    reader = {"Authorization": f"Bearer {pruefer_token}"}

    forbidden = client.post(
        "/api/v1/document-categories", headers=reader, json={"company_id": 1, "name": "Quittung"}
    )
    assert forbidden.status_code == 403

    created = client.post(
        "/api/v1/document-categories", headers=writer, json={"company_id": 1, "name": "Quittung"}
    )
    assert created.status_code == 201
    quittung = created.get_json()
    assert quittung["name"] == "Quittung" and quittung["is_default"] is False

    duplicate = client.post(
        "/api/v1/document-categories", headers=writer, json={"company_id": 1, "name": "QUITTUNG"}
    )
    assert duplicate.status_code == 422

    invoice_id = _category_id(app, "Rechnung")
    uploaded = client.post(
        "/api/v1/documents",
        headers=writer,
        json={
            "company_id": 1,
            "journal_entry_id": 1,
            "category_id": invoice_id,
            "document_date": "2026-04-04",
            "file_name": "rechnung.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_document_bytes(b"api-rechnung")).decode(),
        },
    )
    assert uploaded.status_code == 201
    assert uploaded.get_json()["category_name"] == "Rechnung"

    second = client.post(
        "/api/v1/documents",
        headers=writer,
        json={
            "company_id": 1,
            "journal_entry_id": 1,
            "document_date": "2026-04-05",
            "file_name": "quittung.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_document_bytes(b"api-quittung")).decode(),
        },
    )
    assert second.status_code == 201
    second_id = second.get_json()["id"]

    categorized = client.post(
        f"/api/v1/documents/{second_id}/category",
        headers=writer,
        json={"category_id": quittung["id"]},
    )
    assert categorized.status_code == 200
    assert categorized.get_json()["category_id"] == quittung["id"]

    by_category = client.get(
        f"/api/v1/documents?company_id=1&category_id={quittung['id']}", headers=reader
    )
    assert [d["id"] for d in by_category.get_json()["documents"]] == [second_id]

    entries = client.get("/api/v1/journal-entries?company_id=1", headers=reader).get_json()
    documents = entries["entries"][0]["documents"]
    assert [(d["file_name"], d["category_name"]) for d in documents] == [
        ("rechnung.pdf", "Rechnung"),
        ("quittung.pdf", "Quittung"),
    ]

    deactivated = client.post(
        f"/api/v1/document-categories/{quittung['id']}/active",
        headers=writer,
        json={"is_active": False},
    )
    assert deactivated.status_code == 200
    assert deactivated.get_json()["is_active"] is False

    active_only = client.get(
        "/api/v1/document-categories?company_id=1&include_inactive=false", headers=reader
    )
    assert "Quittung" not in [c["name"] for c in active_only.get_json()["categories"]]

    blocked = client.post(
        f"/api/v1/documents/{second_id}/category",
        headers=writer,
        json={"category_id": quittung["id"]},
    )
    # Unveränderte Zuordnung ist ein No-op, auch wenn die Kategorie inzwischen
    # deaktiviert ist; eine neue Zuordnung wird abgelehnt.
    assert blocked.status_code == 200
    rejected = client.post(
        f"/api/v1/documents/{uploaded.get_json()['id']}/category",
        headers=writer,
        json={"category_id": quittung["id"]},
    )
    assert rejected.status_code == 422

    cleared = client.post(
        f"/api/v1/documents/{second_id}/category", headers=writer, json={"category_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["category_id"] is None


def test_migration_seeds_categories_for_existing_companies(tmp_path):
    db_path = tmp_path / "migrate.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    config = _alembic_config(engine)
    command.upgrade(config, "20261011_0046")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenant (id, name, created_at) VALUES (1, 'Alt', :now)"),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO company (id, tenant_id, name, currency_code, created_at) "
                "VALUES (1, 1, 'Alt GmbH', 'EUR', :now)"
            ),
            {"now": now},
        )

    command.upgrade(config, "20261012_0047")
    with engine.connect() as connection:
        names = connection.execute(
            text("SELECT name FROM document_category WHERE company_id = 1 ORDER BY sort_order")
        ).scalars().all()
    assert names == list(DEFAULT_DOCUMENT_CATEGORIES)

    command.downgrade(config, "20261011_0046")
    command.upgrade(config, "20261012_0047")
