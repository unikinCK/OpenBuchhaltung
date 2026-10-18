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
from domain.models import (
    Account,
    AuditLog,
    ControllingUnit,
    Document,
    JournalEntry,
    JournalEntryLine,
    User,
)

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
                kind="input",
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
        expense = session.get(Account, expense_id)
        cost_center = ControllingUnit(
            tenant_id=expense.tenant_id,
            company_id=expense.company_id,
            unit_type="cost_center",
            code="K500",
            name="Beschaffung",
        )
        profit_center = ControllingUnit(
            tenant_id=expense.tenant_id,
            company_id=expense.company_id,
            unit_type="profit_center",
            code="P500",
            name="Kerngeschäft",
        )
        session.add_all([cost_center, profit_center])
        session.commit()
        cost_center_id = cost_center.id
        profit_center_id = profit_center.id

    xml_bytes = (DEMO_DIR / "erechnung_cii.xml").read_bytes()
    response = client.post(
        "/erechnung/buchen",
        data={
            "company_id": "1",
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "tax_code_id": str(tax_code_id),
            "cost_center_id": str(cost_center_id),
            "profit_center_id": str(profit_center_id),
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
        assert by_code["4200"].cost_center_id == cost_center_id
        assert by_code["4200"].profit_center_id == profit_center_id
        assert by_code["1576"].cost_center_id is None
        assert by_code["1600"].profit_center_id is None

        document = session.execute(select(Document)).scalar_one()
        assert document.journal_entry_id == entry.id
        assert document.file_name == "rechnung.xml"
        assert document.category.name == "Rechnung"


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
        expense = session.get(Account, expense_id)
        cost_center = ControllingUnit(
            tenant_id=expense.tenant_id,
            company_id=expense.company_id,
            unit_type="cost_center",
            code="K501",
            name="API-Beschaffung",
        )
        profit_center = ControllingUnit(
            tenant_id=expense.tenant_id,
            company_id=expense.company_id,
            unit_type="profit_center",
            code="P501",
            name="API-Kerngeschäft",
        )
        session.add_all([cost_center, profit_center])
        session.commit()
        cost_center_id = cost_center.id
        profit_center_id = profit_center.id

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
            "cost_center_id": cost_center_id,
            "profit_center_id": profit_center_id,
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
        assert document.category.name == "Rechnung"
        expense_line = next(line for line in entry.lines if line.account_id == expense_id)
        other_lines = [line for line in entry.lines if line.account_id != expense_id]
        assert expense_line.cost_center_id == cost_center_id
        assert expense_line.profit_center_id == profit_center_id
        assert all(line.cost_center_id is None for line in other_lines)
        assert all(line.profit_center_id is None for line in other_lines)
        audit = session.execute(select(AuditLog).where(AuditLog.entity_type == "einvoice"))
        assert audit.scalar_one().action == "imported"


# --- Mehrere Steuersätze (7 % / 19 %) -------------------------------------

_CII_TAX_19 = """      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>190.00</ram:CalculatedAmount>"""
_CII_TAX_7 = """      <ram:ApplicableTradeTax>
        <ram:CalculatedAmount>7.00</ram:CalculatedAmount>
        <ram:TypeCode>VAT</ram:TypeCode>
        <ram:BasisAmount>100.00</ram:BasisAmount>
        <ram:CategoryCode>AA</ram:CategoryCode>
        <ram:RateApplicablePercent>7.00</ram:RateApplicablePercent>
      </ram:ApplicableTradeTax>
"""


def _multi_rate_cii(grand_total: str = "1297.00") -> bytes:
    xml = (DEMO_DIR / "erechnung_cii.xml").read_text(encoding="utf-8")
    xml = xml.replace(_CII_TAX_19, _CII_TAX_7 + _CII_TAX_19)
    xml = xml.replace(
        "<ram:LineTotalAmount>1000.00", "<ram:LineTotalAmount>1100.00"
    ).replace("<ram:TaxBasisTotalAmount>1000.00", "<ram:TaxBasisTotalAmount>1100.00")
    xml = xml.replace('"EUR">190.00</ram:TaxTotalAmount>', '"EUR">197.00</ram:TaxTotalAmount>')
    xml = xml.replace("<ram:GrandTotalAmount>1190.00", f"<ram:GrandTotalAmount>{grand_total}")
    xml = xml.replace("<ram:DuePayableAmount>1190.00", f"<ram:DuePayableAmount>{grand_total}")
    return xml.encode()


def _multi_rate_ubl() -> bytes:
    xml = (DEMO_DIR / "erechnung_ubl.xml").read_text(encoding="utf-8")
    subtotal_7 = """<cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">50.00</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">3.50</cbc:TaxAmount>
      <cac:TaxCategory>
        <cbc:ID>S</cbc:ID>
        <cbc:Percent>7.00</cbc:Percent>
      </cac:TaxCategory>
    </cac:TaxSubtotal>
    <cac:TaxSubtotal>"""
    xml = xml.replace("<cac:TaxSubtotal>", subtotal_7, 1)
    xml = xml.replace(
        '<cbc:TaxAmount currencyID="EUR">38.00</cbc:TaxAmount>\n    <cac:TaxSubtotal>',
        '<cbc:TaxAmount currencyID="EUR">41.50</cbc:TaxAmount>\n    <cac:TaxSubtotal>',
        1,
    )
    xml = xml.replace(
        '<cbc:LineExtensionAmount currencyID="EUR">200.00',
        '<cbc:LineExtensionAmount currencyID="EUR">250.00',
    ).replace(
        '<cbc:TaxExclusiveAmount currencyID="EUR">200.00',
        '<cbc:TaxExclusiveAmount currencyID="EUR">250.00',
    )
    xml = xml.replace(
        '<cbc:TaxInclusiveAmount currencyID="EUR">238.00',
        '<cbc:TaxInclusiveAmount currencyID="EUR">291.50',
    ).replace(
        '<cbc:PayableAmount currencyID="EUR">238.00', '<cbc:PayableAmount currencyID="EUR">291.50'
    )
    return xml.encode()


def _add_tax_code(session, *, code: str, rate: str, account_code: str) -> int:
    from domain.models import Company, TaxCode

    company = session.execute(select(Company)).scalar_one()
    account = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code=account_code,
        name=f"Vorsteuer {rate} % ({code})",
        account_type="asset",
    )
    session.add(account)
    session.flush()
    tax_code = TaxCode(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code=code,
        kind="input",
        rate=Decimal(rate),
        description=f"Vorsteuer {rate} %",
        vat_account_id=account.id,
    )
    session.add(tax_code)
    session.commit()
    return tax_code.id


def _account_ids(session, *codes: str) -> list[int]:
    return [
        session.execute(select(Account.id).where(Account.code == code)).scalar_one()
        for code in codes
    ]


def _entry_lines(session) -> list[tuple[str, Decimal, Decimal, str | None]]:
    entry = session.execute(select(JournalEntry)).scalar_one()
    return [
        (
            session.get(Account, line.account_id).code,
            line.debit_amount,
            line.credit_amount,
            line.description,
        )
        for line in sorted(entry.lines, key=lambda line: line.line_number)
    ]


def test_parse_multi_rate_invoices():
    cii = parse_einvoice(_multi_rate_cii())
    assert [(line.rate, line.basis, line.tax_amount) for line in cii.tax_lines] == [
        (Decimal("7.00"), Decimal("100.00"), Decimal("7.00")),
        (Decimal("19.00"), Decimal("1000.00"), Decimal("190.00")),
    ]
    assert cii.grand_total == Decimal("1297.00")
    ubl = parse_einvoice(_multi_rate_ubl())
    assert [(line.rate, line.basis, line.tax_amount) for line in ubl.tax_lines] == [
        (Decimal("7.00"), Decimal("50.00"), Decimal("3.50")),
        (Decimal("19.00"), Decimal("200.00"), Decimal("38.00")),
    ]
    assert ubl.tax_total == Decimal("41.50")


def test_parse_zugferd1_applicable_percent():
    xml = (DEMO_DIR / "erechnung_cii.xml").read_bytes().replace(
        b"RateApplicablePercent", b"ApplicablePercent"
    )
    assert parse_einvoice(xml).primary_tax_rate == Decimal("19.00")


def test_einvoice_upload_books_each_tax_rate(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)

    with app.extensions["db_session_factory"]() as session:
        _add_tax_code(session, code="VSt7", rate="7.00", account_code="1571")
        expense_id, creditor_id = _account_ids(session, "4200", "1600")
        from domain.models import TaxCode

        tax_code_19 = session.execute(
            select(TaxCode.id).where(TaxCode.code == "VSt19")
        ).scalar_one()

    page = client.get("/erechnung?company_id=1")
    assert "Steuercode für 7.00 %".encode() in page.data
    assert "Steuercode für 19.00 %".encode() in page.data

    response = client.post(
        "/erechnung/buchen",
        data={
            "company_id": "1",
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "tax_code_id": str(tax_code_19),
            "rate_tax_rate": ["7.00", "19.00"],
            "rate_tax_code_id": ["", ""],
            "einvoice_xml": (BytesIO(_multi_rate_cii()), "mehrsatz.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"verbucht als" in response.data

    with app.extensions["db_session_factory"]() as session:
        lines = _entry_lines(session)
        assert [(code, debit, credit) for code, debit, credit, _ in lines] == [
            ("4200", Decimal("100.00"), Decimal("0.00")),
            ("1571", Decimal("7.00"), Decimal("0.00")),
            ("4200", Decimal("1000.00"), Decimal("0.00")),
            ("1576", Decimal("190.00"), Decimal("0.00")),
            ("1600", Decimal("0.00"), Decimal("1297.00")),
        ]
        assert lines[0][3] == "Muster Lieferant GmbH RE-2026-0815 (7 %)"
        assert lines[1][3] == "Vorsteuer (VSt7)"
        audit = session.execute(
            select(AuditLog).where(AuditLog.entity_type == "einvoice")
        ).scalar_one()
        assert [line["rate"] for line in audit.payload["tax_lines"]] == ["7.00", "19.00"]


def test_einvoice_upload_uses_selected_tax_code_per_rate(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)

    with app.extensions["db_session_factory"]() as session:
        _add_tax_code(session, code="VSt7", rate="7.00", account_code="1571")
        special_7 = _add_tax_code(session, code="VSt7X", rate="7.00", account_code="1572")
        expense_id, creditor_id = _account_ids(session, "4200", "1600")

    response = client.post(
        "/erechnung/buchen",
        data={
            "company_id": "1",
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "tax_code_id": "",
            "rate_tax_rate": ["7.00", "19.00"],
            "rate_tax_code_id": [str(special_7), ""],
            "einvoice_xml": (BytesIO(_multi_rate_cii()), "mehrsatz.xml"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"verbucht als" in response.data
    with app.extensions["db_session_factory"]() as session:
        debits = {code: debit for code, debit, _credit, _ in _entry_lines(session) if debit}
        assert debits["1572"] == Decimal("7.00")
        assert "1571" not in debits
        # 19 % ohne Auswahl und ohne Steuercode oben: Vorsteuer-Code zum Satz.
        assert debits["1576"] == Decimal("190.00")


def test_einvoice_api_books_each_tax_rate_with_assignments(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4650", "name": "Bewirtung", "account_type": "expense"},
    )

    with app.extensions["db_session_factory"]() as session:
        tax_code_7 = _add_tax_code(session, code="VSt7", rate="7.00", account_code="1571")
        expense_id, creditor_id, hospitality_id = _account_ids(session, "4200", "1600", "4650")

    response = client.post(
        "/api/v1/einvoices/import",
        json={
            "company_id": 1,
            "expense_account_id": expense_id,
            "creditor_account_id": creditor_id,
            "tax_lines": [
                {"rate": "7", "tax_code_id": tax_code_7, "expense_account_id": hospitality_id}
            ],
            "file_name": "mehrsatz.xml",
            "mime_type": "application/xml",
            "content_base64": base64.b64encode(_multi_rate_ubl()).decode("ascii"),
        },
    )
    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert len(payload["invoice"]["tax_lines"]) == 2
    assert payload["invoice"]["grand_total"] == "291.50"

    with app.extensions["db_session_factory"]() as session:
        assert [(code, debit, credit) for code, debit, credit, _ in _entry_lines(session)] == [
            ("4650", Decimal("50.00"), Decimal("0.00")),
            ("1571", Decimal("3.50"), Decimal("0.00")),
            ("4200", Decimal("200.00"), Decimal("0.00")),
            ("1576", Decimal("38.00"), Decimal("0.00")),
            ("1600", Decimal("0.00"), Decimal("291.50")),
        ]


def test_einvoice_api_multi_rate_without_tax_code_for_rate(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    _setup_company(client)
    with app.extensions["db_session_factory"]() as session:
        expense_id, creditor_id = _account_ids(session, "4200", "1600")

    body = {
        "company_id": 1,
        "expense_account_id": expense_id,
        "creditor_account_id": creditor_id,
        "file_name": "mehrsatz.xml",
        "content_base64": base64.b64encode(_multi_rate_cii()).decode("ascii"),
    }
    # Kein Vorsteuer-Code mit 7 % vorhanden.
    response = client.post("/api/v1/einvoices/import", json=body)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Tax code not found."

    mismatch = dict(
        body, content_base64=base64.b64encode(_multi_rate_cii("1300.00")).decode("ascii")
    )
    response = client.post("/api/v1/einvoices/import", json=mismatch)
    assert response.status_code == 422
    assert "passt nicht zum Rechnungsbetrag" in response.get_json()["error"]

    response = client.post(
        "/api/v1/einvoices/import", json=dict(body, tax_lines=[{"tax_code_id": 1}])
    )
    assert response.status_code == 400

    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(JournalEntry)).first() is None
