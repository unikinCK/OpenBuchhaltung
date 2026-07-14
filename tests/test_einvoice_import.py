from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from app import create_app
from app.auth import hash_password
from app.services.einvoice_import import EInvoiceParseError, parse_einvoice
from domain.models import Account, AuditLog, Document, JournalEntry, JournalEntryLine, User

DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"


def test_parse_cii_invoice():
    invoice = parse_einvoice((DEMO_DIR / "erechnung_cii.xml").read_bytes())
    assert invoice.syntax == "CII"
    assert invoice.invoice_number == "RE-2026-0815"
    assert invoice.issue_date == date(2026, 6, 15)
    assert invoice.seller_name == "Muster Lieferant GmbH"
    assert invoice.net_total == Decimal("1000.00")
    assert invoice.tax_total == Decimal("190.00")
    assert invoice.grand_total == Decimal("1190.00")
    assert invoice.currency_code == "EUR"
    assert invoice.primary_tax_rate == Decimal("19.00")


def test_parse_ubl_invoice():
    invoice = parse_einvoice((DEMO_DIR / "erechnung_ubl.xml").read_bytes())
    assert invoice.syntax == "UBL"
    assert invoice.invoice_number == "ER-2026-4711"
    assert invoice.issue_date == date(2026, 6, 20)
    assert invoice.seller_name == "Bürobedarf Handels AG"
    assert invoice.net_total == Decimal("200.00")
    assert invoice.tax_total == Decimal("38.00")
    assert invoice.grand_total == Decimal("238.00")
    assert invoice.primary_tax_rate == Decimal("19.00")


def test_parse_rejects_unknown_root():
    with pytest.raises(EInvoiceParseError, match="Unbekanntes Rechnungsformat"):
        parse_einvoice(b"<Foo><Bar/></Foo>")


def test_parse_rejects_broken_xml():
    with pytest.raises(EInvoiceParseError, match="konnte nicht gelesen"):
        parse_einvoice(b"<Invoice><unclosed>")


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_einvoice.db'}",
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


def _setup_company(client):
    client.post("/tenants", data={"tenant_name": "E Mandant", "company_name": "E GmbH"})
    for code, name, atype in (
        ("4200", "Miete", "expense"),
        ("1576", "Vorsteuer 19 %", "asset"),
        ("1600", "Verbindlichkeiten", "liability"),
    ):
        client.post(
            "/accounts",
            data={"company_id": "1", "code": code, "name": name, "account_type": atype},
        )
    # Steuercode VSt19 mit Vorsteuerkonto anlegen
    with client.application.extensions["db_session_factory"]() as session:
        from domain.models import Company, TaxCode

        company = session.execute(select(Company)).scalar_one()
        vat_account = session.execute(
            select(Account).where(Account.code == "1576")
        ).scalar_one()
        session.add(
            TaxCode(
                tenant_id=company.tenant_id,
                company_id=company.id,
                code="VSt19",
                rate=Decimal("19.00"),
                description="Vorsteuer 19 %",
                vat_account_id=vat_account.id,
            )
        )
        session.commit()


def test_einvoice_upload_books_entry_and_stores_document(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)

    with app.extensions["db_session_factory"]() as session:
        expense_id = session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()
        creditor_id = session.execute(
            select(Account.id).where(Account.code == "1600")
        ).scalar_one()
        from domain.models import TaxCode

        tax_code_id = session.execute(select(TaxCode.id)).scalar_one()

    xml_bytes = (DEMO_DIR / "erechnung_cii.xml").read_bytes()
    response = client.post(
        "/erechnung/buchen",
        data={
            "company_id": "1",
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "tax_code_id": str(tax_code_id),
            "einvoice_xml": (BytesIO(xml_bytes), "rechnung.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"verbucht als" in response.data

    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(select(JournalEntry)).scalar_one()
        assert entry.entry_date == date(2026, 6, 15)
        lines = (
            session.execute(
                select(JournalEntryLine)
                .where(JournalEntryLine.journal_entry_id == entry.id)
                .order_by(JournalEntryLine.line_number)
            )
            .scalars()
            .all()
        )
        by_code = {session.get(Account, line.account_id).code: line for line in lines}
        assert by_code["4200"].debit_amount == Decimal("1000.00")
        assert by_code["1576"].debit_amount == Decimal("190.00")
        assert by_code["1600"].credit_amount == Decimal("1190.00")

        document = session.execute(select(Document)).scalar_one()
        assert document.journal_entry_id == entry.id
        assert document.file_name == "rechnung.xml"


def test_einvoice_upload_rejects_broken_xml(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)

    with app.extensions["db_session_factory"]() as session:
        expense_id = session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()
        creditor_id = session.execute(
            select(Account.id).where(Account.code == "1600")
        ).scalar_one()

    response = client.post(
        "/erechnung/buchen",
        data={
            "company_id": "1",
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "einvoice_xml": (BytesIO(b"<Invoice><broken>"), "kaputt.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "konnte nicht gelesen".encode() in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(JournalEntry)).first() is None


def test_einvoice_api_import_books_entry_and_stores_document(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)

    with app.extensions["db_session_factory"]() as session:
        expense_id = session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()
        creditor_id = session.execute(
            select(Account.id).where(Account.code == "1600")
        ).scalar_one()
        from domain.models import TaxCode

        tax_code_id = session.execute(select(TaxCode.id)).scalar_one()

    xml_bytes = (DEMO_DIR / "erechnung_cii.xml").read_bytes()
    response = client.post(
        "/api/v1/einvoices/import",
        json={
            "company_id": 1,
            "expense_account_id": expense_id,
            "creditor_account_id": creditor_id,
            "tax_code_id": tax_code_id,
            "file_name": "rechnung.xml",
            "mime_type": "application/xml",
            "content_base64": base64.b64encode(xml_bytes).decode("ascii"),
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["invoice"]["invoice_number"] == "RE-2026-0815"
    assert payload["invoice"]["grand_total"] == "1190.00"

    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(select(JournalEntry)).scalar_one()
        document = session.execute(select(Document)).scalar_one()
        assert payload["journal_entry_id"] == entry.id
        assert payload["document_id"] == document.id
        assert document.journal_entry_id == entry.id
        assert document.document_date == date(2026, 6, 15)
        assert entry.entry_date == date(2026, 6, 15)
        audit = session.execute(select(AuditLog).where(AuditLog.entity_type == "einvoice"))
        assert audit.scalar_one().action == "imported"
