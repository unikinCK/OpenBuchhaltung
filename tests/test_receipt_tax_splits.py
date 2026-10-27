"""Belegbuchung mit mehreren Steuersätzen (Restaurant 7 %/19 %) und Trinkgeld.

Service (:func:`book_incoming_invoice`), Kontierungsvorschlag, Beleg-OCR und
Belegabgleich über REST, UI und MCP-Schema.
"""

import base64
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from document_files import pdf_document_bytes
from test_receipt_matching import (
    _account_id,
    _create_test_app,
    _logged_in_client,
    _seed_company_with_accounts,
    _upload_document,
)

from app.services import receipt_matching, receipt_ocr
from app.services.incoming_invoice import (
    IncomingInvoiceError,
    InvoiceTaxSplit,
    book_incoming_invoice,
    parse_tax_splits,
)
from app.services.mcp_server import TOOLS_BY_NAME
from app.services.receipt_matching import create_match_suggestion
from app.services.receipt_proposals import propose_tax_lines
from domain.models import (
    Account,
    AuditLog,
    Company,
    Document,
    JournalEntry,
    ReceiptMatchSuggestion,
    TaxCode,
)

# Restaurant: 100,00 netto zu 7 % + 10,00 netto zu 19 %, Brutto 118,90, Trinkgeld 11,10.
RESTAURANT_TAX_LINES = [
    (Decimal("7"), Decimal("100.00"), Decimal("7.00")),
    (Decimal("19"), Decimal("10.00"), Decimal("1.90")),
]


def _add_seven_percent_code(app, company_id: int) -> tuple[int, int, int]:
    """Legt Vorsteuer 7 % an; liefert (VSt7-, VSt19-Code, 1571-Konto)."""
    with app.extensions["db_session_factory"]() as session:
        company = session.get(Company, company_id)
        vat7 = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code="1571",
            name="Vorsteuer 7%",
            account_type="asset",
        )
        session.add(vat7)
        session.flush()
        code7 = TaxCode(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code="VSt7",
            kind="input",
            rate=Decimal("7.00"),
            vat_account_id=vat7.id,
        )
        session.add(code7)
        session.commit()
        code19 = session.query(TaxCode).filter_by(company_id=company.id, code="VSt19").one()
        return code7.id, code19.id, vat7.id


def _restaurant_extraction(**kwargs) -> receipt_ocr.ReceiptExtraction:
    return receipt_ocr.ReceiptExtraction(
        raw_text="Muster Restaurant",
        supplier="Muster Restaurant",
        invoice_number="B-17",
        invoice_date=date(2026, 10, 3),
        net_amount=Decimal("110.00"),
        tax_amount=Decimal("8.90"),
        gross_amount=Decimal("118.90"),
        tax_rate=Decimal("7.00"),
        tax_lines=list(RESTAURANT_TAX_LINES),
        tip_amount=Decimal("11.10"),
        description="Restaurantbesuch",
    )


def _assert_restaurant_entry(entry: JournalEntry, *, expense_id, creditor_id, vat7_id, vat19_id):
    debit = {}
    for line in entry.lines:
        if line.debit_amount:
            debit.setdefault(line.account_id, []).append(line.debit_amount)
    assert sorted(debit[expense_id]) == [Decimal("10.00"), Decimal("11.10"), Decimal("100.00")]
    assert debit[vat7_id] == [Decimal("7.00")]
    assert debit[vat19_id] == [Decimal("1.90")]
    credit = [line for line in entry.lines if line.credit_amount]
    assert len(credit) == 1
    assert credit[0].account_id == creditor_id
    assert credit[0].credit_amount == Decimal("130.00")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_book_incoming_invoice_splits_per_rate_and_books_tip(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, vat7_id = _add_seven_percent_code(app, company_id)
    expense_id = _account_id(app, company_id, "6300")
    creditor_id = _account_id(app, company_id, "1600")
    vat19_id = _account_id(app, company_id, "1576")

    with app.extensions["db_session_factory"]() as session:
        entry = book_incoming_invoice(
            session=session,
            company=session.get(Company, company_id),
            entry_date=date(2026, 10, 3),
            description="Muster Restaurant",
            expense_account_id=expense_id,
            creditor_account_id=creditor_id,
            net_amount=Decimal("0"),
            tax_amount=Decimal("0"),
            expense_line_description="Muster Restaurant",
            tax_splits=[
                InvoiceTaxSplit(Decimal("100.00"), Decimal("7.00"), tax_code_id=code7),
                # Ohne Steuercode: Vorsteuer-Code per Steuersatz.
                InvoiceTaxSplit(Decimal("10.00"), Decimal("1.90"), rate=Decimal("19")),
            ],
            tip_amount=Decimal("11.10"),
            changed_by="test",
        )
        _assert_restaurant_entry(
            entry,
            expense_id=expense_id,
            creditor_id=creditor_id,
            vat7_id=vat7_id,
            vat19_id=vat19_id,
        )
        texts = {line.description for line in entry.lines if line.account_id == expense_id}
        assert texts == {
            "Muster Restaurant (7 %)",
            "Muster Restaurant (19 %)",
            "Trinkgeld (ohne USt)",
        }
        assert code19


def test_split_without_matching_tax_code_is_rejected(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    with app.extensions["db_session_factory"]() as session:
        with pytest.raises(IncomingInvoiceError) as excinfo:
            book_incoming_invoice(
                session=session,
                company=session.get(Company, company_id),
                entry_date=date(2026, 10, 3),
                description="Hotel",
                expense_account_id=_account_id(app, company_id, "6300"),
                creditor_account_id=_account_id(app, company_id, "1600"),
                net_amount=Decimal("0"),
                tax_amount=Decimal("0"),
                tax_splits=[
                    InvoiceTaxSplit(Decimal("100.00"), Decimal("7.00"), rate=Decimal("7")),
                    InvoiceTaxSplit(Decimal("20.00"), Decimal("3.80"), rate=Decimal("19")),
                ],
                changed_by="test",
            )
        assert excinfo.value.code == "tax_code_not_found"
        assert "7 %" in str(excinfo.value)


def test_parse_tax_splits_skips_empty_rows_and_rejects_garbage():
    splits = parse_tax_splits(
        [
            {"net_amount": "100.00", "tax_amount": "7.00", "tax_code_id": "3"},
            {"net_amount": "", "tax_amount": ""},
            {"net_amount": "10.00", "tax_amount": "1.90", "rate": "19", "expense_account_id": 9},
        ]
    )
    assert splits == [
        InvoiceTaxSplit(Decimal("100.00"), Decimal("7.00"), tax_code_id=3),
        InvoiceTaxSplit(
            Decimal("10.00"), Decimal("1.90"), rate=Decimal("19"), expense_account_id=9
        ),
    ]
    assert parse_tax_splits(None) is None
    assert parse_tax_splits([{"net_amount": "0", "tax_amount": "0"}]) is None
    with pytest.raises(ValueError):
        parse_tax_splits({"net_amount": "1"})
    with pytest.raises(ValueError):
        parse_tax_splits([{"net_amount": "abc", "tax_amount": "1"}])


def test_propose_tax_lines_needs_two_rates_and_resolves_codes(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, _vat7 = _add_seven_percent_code(app, company_id)
    with app.extensions["db_session_factory"]() as session:
        lines = propose_tax_lines(session, company_id, RESTAURANT_TAX_LINES)
        assert [line.as_dict() for line in lines] == [
            {
                "rate": "7",
                "net_amount": "100.00",
                "tax_amount": "7.00",
                "tax_code_id": code7,
                "tax_code": "VSt7",
            },
            {
                "rate": "19",
                "net_amount": "10.00",
                "tax_amount": "1.90",
                "tax_code_id": code19,
                "tax_code": "VSt19",
            },
        ]
        single = [(Decimal("19"), Decimal("50.00"), Decimal("4.75"))] * 2
        assert propose_tax_lines(session, company_id, single) == ()
        assert propose_tax_lines(
            session, company_id, RESTAURANT_TAX_LINES, reverse_charge=True
        ) == ()


# ---------------------------------------------------------------------------
# Beleg-OCR: REST und UI
# ---------------------------------------------------------------------------


def test_receipt_ocr_api_books_tax_lines_and_tip(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, vat7_id = _add_seven_percent_code(app, company_id)
    client = _logged_in_client(app)
    monkeypatch.setattr(
        "app.api.receipt_ocr.analyze_document", lambda **kwargs: _restaurant_extraction()
    )

    suggest = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "document_date": "2026-10-03",
            "file_name": "restaurant.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_document_bytes(b"api")).decode("ascii"),
        },
    )
    assert suggest.status_code == 201, suggest.get_json()
    body = suggest.get_json()
    proposal_lines = body["booking_proposal"]["tax_lines"]
    assert [line["tax_code_id"] for line in proposal_lines] == [code7, code19]

    expense_id = _account_id(app, company_id, "6300")
    creditor_id = _account_id(app, company_id, "1600")
    book = client.post(
        "/api/v1/receipt-ocr/book",
        json={
            "company_id": company_id,
            "document_id": body["document_id"],
            "expense_account_id": expense_id,
            "creditor_account_id": creditor_id,
            "entry_date": "2026-10-03",
            "description": "Muster Restaurant",
            "tax_lines": proposal_lines,
            "tip_amount": body["extraction"]["tip_amount"],
        },
    )
    assert book.status_code == 201, book.get_json()
    assert book.get_json()["gross_amount"] == "130.00"

    with app.extensions["db_session_factory"]() as session:
        entry = session.query(JournalEntry).one()
        _assert_restaurant_entry(
            entry,
            expense_id=expense_id,
            creditor_id=creditor_id,
            vat7_id=vat7_id,
            vat19_id=_account_id(app, company_id, "1576"),
        )
        audit = (
            session.query(AuditLog)
            .filter_by(entity_type="document", action="ocr_booked")
            .one()
        )
        assert len(audit.payload["tax_lines"]) == 2
        assert audit.payload["tip_amount"] == "11.10"


def test_receipt_ocr_api_rejects_invalid_tax_lines(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    client = _logged_in_client(app)
    response = client.post(
        "/api/v1/receipt-ocr/book",
        json={
            "company_id": company_id,
            "document_id": 1,
            "expense_account_id": _account_id(app, company_id, "6300"),
            "creditor_account_id": _account_id(app, company_id, "1600"),
            "tax_lines": "7 % und 19 %",
        },
    )
    assert response.status_code == 400


def test_receipt_ocr_ui_shows_split_rows_and_books_them(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, vat7_id = _add_seven_percent_code(app, company_id)
    client = _logged_in_client(app)
    monkeypatch.setattr(
        "app.web.receipt_ocr.analyze_document", lambda **kwargs: _restaurant_extraction()
    )
    page = client.post(
        "/belege/ocr/vorschlag",
        data={
            "company_id": str(company_id),
            "document_date": "2026-10-03",
            "document_file": (BytesIO(pdf_document_bytes(b"ui")), "restaurant.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert page.status_code == 200
    html = page.data.decode("utf-8")
    assert "Aufteilung nach Steuersätzen" in html
    assert html.count('name="split_net_amount"') == 3  # zwei Sätze + Leerzeile
    assert 'value="100.00"' in html
    assert 'name="tip_amount"' in html and 'value="11.10"' in html
    assert 'name="net_amount"' not in html

    with app.extensions["db_session_factory"]() as session:
        document_id = session.query(Document).one().id
    expense_id = _account_id(app, company_id, "6300")
    creditor_id = _account_id(app, company_id, "1600")
    booked = client.post(
        "/belege/ocr/buchen",
        data={
            "company_id": str(company_id),
            "document_id": str(document_id),
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "entry_date": "2026-10-03",
            "description": "Muster Restaurant",
            "split_net_amount": ["100,00", "10,00", ""],
            "split_tax_code_id": [str(code7), str(code19), ""],
            "split_tax_amount": ["7,00", "1,90", ""],
            "split_expense_account_id": ["", "", ""],
            "tip_amount": "11,10",
        },
        follow_redirects=True,
    )
    assert booked.status_code == 200
    assert "brutto 130,00" in booked.data.decode("utf-8")
    with app.extensions["db_session_factory"]() as session:
        entry = session.query(JournalEntry).one()
        _assert_restaurant_entry(
            entry,
            expense_id=expense_id,
            creditor_id=creditor_id,
            vat7_id=vat7_id,
            vat19_id=_account_id(app, company_id, "1576"),
        )
        assert session.get(Document, document_id).journal_entry_id == entry.id


# ---------------------------------------------------------------------------
# Belegabgleich: Persistenz, REST und UI
# ---------------------------------------------------------------------------


def _restaurant_suggestion(app, monkeypatch, company_id: int) -> int:
    document_id = _upload_document(app, company_id, ["Muster Restaurant"], "restaurant.pdf")
    monkeypatch.setattr(
        receipt_matching, "analyze_document", lambda **kwargs: _restaurant_extraction()
    )
    with app.extensions["db_session_factory"]() as session:
        suggestion = create_match_suggestion(
            session=session, company_id=company_id, document_id=document_id, changed_by="t"
        )
        return suggestion.id


def test_match_suggestion_persists_tax_lines_and_books_via_api(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, vat7_id = _add_seven_percent_code(app, company_id)
    suggestion_id = _restaurant_suggestion(app, monkeypatch, company_id)

    with app.extensions["db_session_factory"]() as session:
        suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
        assert suggestion.suggestion_type == "new_booking"
        assert suggestion.tax_lines == [
            {"rate": "7", "net_amount": "100.00", "tax_amount": "7.00", "tax_code_id": code7},
            {"rate": "19", "net_amount": "10.00", "tax_amount": "1.90", "tax_code_id": code19},
        ]

    client = _logged_in_client(app)
    listed = client.get(f"/api/v1/receipt-matching/suggestions?company_id={company_id}")
    item = next(s for s in listed.get_json()["items"] if s["id"] == suggestion_id)
    proposal_lines = item["booking_proposal"]["tax_lines"]
    assert len(proposal_lines) == 2

    expense_id = _account_id(app, company_id, "6300")
    creditor_id = _account_id(app, company_id, "1600")
    approved = client.post(
        f"/api/v1/receipt-matching/suggestions/{suggestion_id}/approve",
        json={
            "company_id": company_id,
            "expense_account_id": expense_id,
            "creditor_account_id": creditor_id,
            "tax_lines": proposal_lines,
            "tip_amount": item["tip_amount"],
        },
    )
    assert approved.status_code == 200, approved.get_json()
    with app.extensions["db_session_factory"]() as session:
        entry = session.get(JournalEntry, approved.get_json()["journal_entry_id"])
        _assert_restaurant_entry(
            entry,
            expense_id=expense_id,
            creditor_id=creditor_id,
            vat7_id=vat7_id,
            vat19_id=_account_id(app, company_id, "1576"),
        )
        assert entry.entry_date == date(2026, 10, 3)
        audit = (
            session.query(AuditLog)
            .filter_by(entity_type="receipt_match_suggestion", action="booked")
            .one()
        )
        assert audit.payload["gross_amount"] == "130.00"
        assert len(audit.payload["tax_lines"]) == 2


def test_match_ui_renders_split_rows_and_books_them(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    code7, code19, vat7_id = _add_seven_percent_code(app, company_id)
    suggestion_id = _restaurant_suggestion(app, monkeypatch, company_id)
    client = _logged_in_client(app)

    page = client.get(f"/belege/abgleich?company_id={company_id}")
    html = page.data.decode("utf-8")
    assert "davon 7 %" in html
    assert "Aufteilung nach Steuersätzen" in html
    assert f'id="split_net_amount_{suggestion_id}_1"' in html

    expense_id = _account_id(app, company_id, "6300")
    creditor_id = _account_id(app, company_id, "1600")
    booked = client.post(
        f"/belege/abgleich/{suggestion_id}/buchen",
        data={
            "company_id": str(company_id),
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "entry_date": "2026-10-03",
            "split_net_amount": ["100.00", "10.00", ""],
            "split_tax_code_id": [str(code7), str(code19), ""],
            "split_tax_amount": ["7.00", "1.90", ""],
            "split_expense_account_id": ["", "", ""],
            "tip_amount": "11.10",
        },
        follow_redirects=True,
    )
    assert booked.status_code == 200
    with app.extensions["db_session_factory"]() as session:
        entry = session.query(JournalEntry).one()
        _assert_restaurant_entry(
            entry,
            expense_id=expense_id,
            creditor_id=creditor_id,
            vat7_id=vat7_id,
            vat19_id=_account_id(app, company_id, "1576"),
        )


def test_mcp_booking_tools_accept_tax_lines_and_tip():
    for name in ("book_receipt_ocr_suggestion", "approve_receipt_match_suggestion"):
        properties = TOOLS_BY_NAME[name].input_schema["properties"]
        assert properties["tax_lines"]["type"] == "array"
        assert properties["tax_lines"]["items"]["required"] == ["net_amount", "tax_amount"]
        assert "tip_amount" in properties
    required = TOOLS_BY_NAME["book_receipt_ocr_suggestion"].input_schema["required"]
    assert "net_amount" not in required
