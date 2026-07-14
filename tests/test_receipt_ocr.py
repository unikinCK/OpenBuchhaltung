import base64
import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest

from app import create_app
from app.auth import hash_password
from app.services import receipt_ocr
from app.services.receipt_ocr import (
    LlmReceiptFields,
    ReceiptLLMError,
    ReceiptOCRError,
    analyze_document,
    analyze_receipt_text,
    apply_llm_control,
    extract_document_text,
    extract_receipt_fields_llm,
)
from domain.models import Account, AuditLog, Company, Document, JournalEntry, TaxCode, Tenant

# ---------------------------------------------------------------------------
# Stufe 2: heuristische Analyse (rein, deterministisch)
# ---------------------------------------------------------------------------

RECEIPT_TEXT = (
    "Muster Lieferant GmbH\n"
    "Rechnung Nr. 2026-4711\n"
    "Rechnungsdatum: 08.07.2026\n"
    "Position A ............ 200,00\n"
    "Nettobetrag        200,00 EUR\n"
    "MwSt 19 %           38,00 EUR\n"
    "Gesamtbetrag       238,00 EUR\n"
)


def test_analyze_full_receipt_extracts_all_fields():
    result = analyze_receipt_text(RECEIPT_TEXT)

    assert result.supplier == "Muster Lieferant GmbH"
    assert result.invoice_number == "2026-4711"
    assert result.invoice_date == date(2026, 7, 8)
    assert result.net_amount == Decimal("200.00")
    assert result.tax_amount == Decimal("38.00")
    assert result.gross_amount == Decimal("238.00")
    assert result.tax_rate == Decimal("19")
    assert result.currency_code == "EUR"
    assert result.confidence == "hoch"
    assert result.warnings == []


def test_percentage_is_not_mistaken_for_tax_amount():
    # Der Steuerbetrag (38,00) darf nicht mit dem Steuersatz (19 %) verwechselt werden.
    result = analyze_receipt_text("Umsatzsteuer 19 % 38,00 EUR")
    assert result.tax_amount == Decimal("38.00")


def test_analyze_derives_net_and_tax_from_gross_and_rate():
    text = "Rechnungsbetrag 119,00 EUR inkl. 19% MwSt"
    result = analyze_receipt_text(text)
    assert result.gross_amount == Decimal("119.00")
    assert result.net_amount == Decimal("100.00")
    assert result.tax_amount == Decimal("19.00")
    assert result.tax_rate == Decimal("19")


def test_analyze_seven_percent_rate():
    text = "Nettobetrag 100,00\nMwSt 7% 7,00\nGesamtbetrag 107,00"
    result = analyze_receipt_text(text)
    assert result.tax_rate == Decimal("7")
    assert result.gross_amount == Decimal("107.00")


def test_analyze_gross_only_suggests_without_tax_and_warns():
    result = analyze_receipt_text("Zahlbetrag 50,00 EUR")
    assert result.gross_amount == Decimal("50.00")
    assert result.net_amount == Decimal("50.00")
    assert result.tax_amount == Decimal("0.00")
    assert result.tax_rate == Decimal("0")
    assert any("Steuersatz" in warning for warning in result.warnings)
    assert result.confidence == "niedrig"


def test_analyze_german_thousands_separator():
    result = analyze_receipt_text("Gesamtbetrag 1.234,56 EUR")
    assert result.gross_amount == Decimal("1234.56")


def test_analyze_flags_inconsistent_totals():
    text = "Nettobetrag 100,00\nUmsatzsteuer 19,00\nGesamtbetrag 200,00"
    result = analyze_receipt_text(text)
    assert any("prüfen" in warning.lower() for warning in result.warnings)


def test_analyze_empty_text():
    result = analyze_receipt_text("   ")
    assert not result.has_booking_basis
    assert result.confidence == "niedrig"


# ---------------------------------------------------------------------------
# Stufe 1: Textgewinnung
# ---------------------------------------------------------------------------


def _pdf_with_text(lines: list[str]) -> bytes:
    """Minimales PDF mit einer echten Textebene (unkomprimierter Content-Stream)."""
    shows = " ".join(f"({line}) Tj 0 -14 Td" for line in lines)
    content = f"BT /F1 12 Tf 50 800 Td {shows} ET".encode("latin-1")
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(content)).encode("ascii") + b">>\nstream\n"
        + content
        + b"\nendstream endobj\n%%EOF"
    )


def test_extract_text_from_plain_text():
    text, source = extract_document_text(
        file_bytes="Gesamtbetrag 10,00".encode("utf-8"),
        mime_type="text/plain",
        file_name="beleg.txt",
    )
    assert source == "text"
    assert "Gesamtbetrag" in text


def test_extract_text_from_pdf_with_text_layer():
    pdf = _pdf_with_text(["Muster Lieferant GmbH", "Gesamtbetrag 238,00 EUR"])
    text, source = extract_document_text(
        file_bytes=pdf, mime_type="application/pdf", file_name="beleg.pdf"
    )
    assert source == "pdf"
    assert "Muster Lieferant GmbH" in text
    assert "238,00" in text


def test_extract_pdf_handles_escaped_parentheses():
    pdf = _pdf_with_text([r"Firma \(Muster\) GmbH", "Gesamtbetrag 100,00 EUR"])
    text, _ = extract_document_text(
        file_bytes=pdf, mime_type="application/pdf", file_name="beleg.pdf"
    )
    assert "Firma (Muster) GmbH" in text


def test_image_without_endpoint_raises():
    with pytest.raises(ReceiptOCRError):
        extract_document_text(
            file_bytes=b"\x89PNG\r\n", mime_type="image/png", file_name="scan.png"
        )


def test_scanned_pdf_without_text_and_endpoint_raises():
    with pytest.raises(ReceiptOCRError):
        extract_document_text(
            file_bytes=b"%PDF-1.4\n%%EOF", mime_type="application/pdf", file_name="scan.pdf"
        )


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_image_uses_ocr_endpoint(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        return _FakeResponse({"output_text": "Gesamtbetrag 119,00 EUR\n19% MwSt"})

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)

    result = analyze_document(
        file_bytes=b"\x89PNG\r\nfakeimage",
        mime_type="image/png",
        file_name="scan.png",
        ocr_endpoint="https://ocr.example/responses",
        ocr_model="test-model",
    )
    assert captured["url"] == "https://ocr.example/responses"
    assert result.source == "ocr-endpoint"
    assert result.gross_amount == Decimal("119.00")
    assert result.net_amount == Decimal("100.00")


def test_ocr_endpoint_unreachable_raises(monkeypatch):
    from urllib.error import URLError

    def fake_urlopen(request, timeout=0):
        raise URLError("boom")

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    with pytest.raises(ReceiptOCRError):
        extract_document_text(
            file_bytes=b"img",
            mime_type="image/png",
            file_name="scan.png",
            ocr_endpoint="https://ocr.example/responses",
        )


# ---------------------------------------------------------------------------
# Stufe 3: LLM als Unterstützung/Fallback und Kontrolle
# ---------------------------------------------------------------------------


def _llm_urlopen(payload_fields: dict):
    def fake_urlopen(request, timeout=0):
        return _FakeResponse({"output_text": json.dumps(payload_fields)})

    return fake_urlopen


def test_extract_receipt_fields_llm_parses_json(monkeypatch):
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        _llm_urlopen(
            {
                "supplier": "ACME AG",
                "invoice_number": "R-77",
                "invoice_date": "2026-07-01",
                "net_amount": 100,
                "tax_amount": 19,
                "gross_amount": 119,
                "tax_rate": 19,
                "currency_code": "EUR",
            }
        ),
    )
    fields = extract_receipt_fields_llm(
        "irgendein text", endpoint_url="https://llm.example/responses", model="m"
    )
    assert isinstance(fields, LlmReceiptFields)
    assert fields.supplier == "ACME AG"
    assert fields.invoice_number == "R-77"
    assert fields.invoice_date == date(2026, 7, 1)
    assert fields.gross_amount == Decimal("119.00")
    assert fields.tax_rate == Decimal("19.00")


def test_extract_receipt_fields_llm_handles_prose_wrapped_json(monkeypatch):
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(
            {"output_text": 'Hier das Ergebnis: {"gross_amount": 50.0} — fertig.'}
        ),
    )
    fields = extract_receipt_fields_llm(
        "text", endpoint_url="https://llm.example/responses", model="m"
    )
    assert fields.gross_amount == Decimal("50.00")


def test_extract_receipt_fields_llm_invalid_json_raises(monkeypatch):
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        lambda request, timeout=0: _FakeResponse({"output_text": "keine daten hier"}),
    )
    with pytest.raises(ReceiptLLMError):
        extract_receipt_fields_llm(
            "text", endpoint_url="https://llm.example/responses", model="m"
        )


def test_control_confirms_matching_gross():
    extraction = analyze_receipt_text(RECEIPT_TEXT)  # gross 238,00 regelbasiert
    apply_llm_control(extraction, LlmReceiptFields(gross_amount=Decimal("238.00")))
    assert extraction.llm_used is True
    assert extraction.control_status == "bestätigt"
    assert extraction.gross_amount == Decimal("238.00")


def test_control_flags_diverging_gross():
    extraction = analyze_receipt_text(RECEIPT_TEXT)
    apply_llm_control(extraction, LlmReceiptFields(gross_amount=Decimal("999.00")))
    assert extraction.control_status == "abweichung"
    assert extraction.confidence == "niedrig"
    assert any("KI-Kontrolle" in w for w in extraction.warnings)


def test_llm_fills_gaps_when_deterministic_finds_nothing():
    # Freitext ohne erkennbare Beträge -> regelbasiert keine Basis.
    extraction = analyze_receipt_text("Belegtext ohne maschinenlesbare Summen")
    assert not extraction.has_booking_basis
    apply_llm_control(
        extraction,
        LlmReceiptFields(
            supplier="Fallback GmbH",
            gross_amount=Decimal("119.00"),
            tax_rate=Decimal("19"),
        ),
    )
    assert extraction.control_status == "ergänzt"
    assert extraction.has_booking_basis
    assert extraction.gross_amount == Decimal("119.00")
    assert extraction.net_amount == Decimal("100.00")
    assert extraction.tax_amount == Decimal("19.00")
    assert extraction.supplier == "Fallback GmbH"
    assert "+llm" in extraction.source


def test_llm_error_is_non_blocking(monkeypatch):
    def boom(request, timeout=0):
        raise receipt_ocr.URLError("down")

    monkeypatch.setattr(receipt_ocr, "urlopen", boom)
    pdf = _pdf_with_text(
        ["Muster Lieferant GmbH", "Nettobetrag 200,00", "MwSt 19 % 38,00", "Gesamtbetrag 238,00"]
    )
    result = analyze_document(
        file_bytes=pdf,
        mime_type="application/pdf",
        file_name="beleg.pdf",
        llm_endpoint="https://llm.example/responses",
    )
    # Regelbasiertes Ergebnis bleibt erhalten, LLM-Fehler nur als Warnung.
    assert result.gross_amount == Decimal("238.00")
    assert any("KI-Kontrolle nicht möglich" in w for w in result.warnings)


def test_analyze_document_runs_llm_control(monkeypatch):
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        _llm_urlopen({"gross_amount": 238.0}),
    )
    pdf = _pdf_with_text(
        ["Muster Lieferant GmbH", "Nettobetrag 200,00", "MwSt 19 % 38,00", "Gesamtbetrag 238,00"]
    )
    result = analyze_document(
        file_bytes=pdf,
        mime_type="application/pdf",
        file_name="beleg.pdf",
        llm_endpoint="https://llm.example/responses",
        llm_model="m",
    )
    assert result.llm_used is True
    assert result.control_status == "bestätigt"


# ---------------------------------------------------------------------------
# Ende-zu-Ende: Route-Flow (Upload -> Vorschlag -> Buchen)
# ---------------------------------------------------------------------------


def _create_test_app(tmp_path: Path, **extra_config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'ocr_app.db'}",
            **extra_config,
        }
    )
    from domain.models import User

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


def _logged_in_client(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return client


def _seed_company_with_accounts(app):
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="OCR-Mandant")
        session.add(tenant)
        session.flush()
        company = Company(tenant_id=tenant.id, name="OCR GmbH")
        session.add(company)
        session.flush()
        expense = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="6300",
            name="Sonstige Aufwendungen",
            account_type="expense",
        )
        vat = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1576",
            name="Vorsteuer 19%",
            account_type="asset",
        )
        creditor = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1600",
            name="Verbindlichkeiten",
            account_type="liability",
        )
        session.add_all([expense, vat, creditor])
        session.flush()
        tax_code = TaxCode(
            tenant_id=tenant.id,
            company_id=company.id,
            code="VSt19",
            rate=Decimal("19.00"),
            vat_account_id=vat.id,
        )
        session.add(tax_code)
        session.commit()
        return company.id


def test_ocr_flow_upload_suggest_and_book(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    client = _logged_in_client(app)

    pdf = _pdf_with_text(
        [
            "Muster Lieferant GmbH",
            "Rechnung Nr. 2026-4711",
            "Rechnungsdatum: 08.07.2026",
            "Nettobetrag 200,00 EUR",
            "MwSt 19 % 38,00 EUR",
            "Gesamtbetrag 238,00 EUR",
        ]
    )

    missing_date = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "file_name": "api-beleg.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf).decode("ascii"),
        },
    )
    assert missing_date.status_code == 400
    assert "document_date" in missing_date.get_json()["error"]

    suggest = client.post(
        "/belege/ocr/vorschlag",
        data={
            "company_id": str(company_id),
            "document_date": "2026-07-07",
            "document_file": (BytesIO(pdf), "beleg.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert suggest.status_code == 200
    body = suggest.data.decode("utf-8")
    assert "Muster Lieferant GmbH" in body
    assert "238,00" in body or "238.00" in body

    with app.extensions["db_session_factory"]() as session:
        document = session.query(Document).one()
        assert document.journal_entry_id is None
        assert document.document_date == date(2026, 7, 8)
        document_id = document.id
        assert any(
            e.action == "ocr_analyzed"
            for e in session.query(AuditLog).filter_by(entity_type="document").all()
        )

    with app.extensions["db_session_factory"]() as session:
        tax_code_id = session.query(TaxCode).one().id
        expense_id = session.query(Account).filter_by(code="6300").one().id
        creditor_id = session.query(Account).filter_by(code="1600").one().id

    book = client.post(
        "/belege/ocr/buchen",
        data={
            "company_id": str(company_id),
            "document_id": str(document_id),
            "expense_account_id": str(expense_id),
            "creditor_account_id": str(creditor_id),
            "tax_code_id": str(tax_code_id),
            "entry_date": "2026-07-08",
            "description": "Muster Lieferant 2026-4711",
            "net_amount": "200.00",
            "tax_amount": "38.00",
        },
        follow_redirects=True,
    )
    assert book.status_code == 200

    with app.extensions["db_session_factory"]() as session:
        entry = session.query(JournalEntry).one()
        assert entry.description == "Muster Lieferant 2026-4711"
        document = session.get(Document, document_id)
        assert document.journal_entry_id == entry.id
        assert document.document_date == entry.entry_date == date(2026, 7, 8)
        total_debit = sum(line.debit_amount for line in entry.lines)
        total_credit = sum(line.credit_amount for line in entry.lines)
        assert total_debit == total_credit == Decimal("238.00")
        assert any(
            e.action == "ocr_booked"
            for e in session.query(AuditLog).filter_by(entity_type="document").all()
        )


def test_receipt_ocr_keeps_fallback_document_date_when_none_is_recognized(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    client = _logged_in_client(app)
    pdf = _pdf_with_text(
        [
            "Muster Lieferant GmbH",
            "Rechnung Nr. OHNE-DATUM",
            "Nettobetrag 100,00 EUR",
            "MwSt 19 % 19,00 EUR",
            "Gesamtbetrag 119,00 EUR",
        ]
    )

    response = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "document_date": "2026-07-07",
            "file_name": "fallback.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf).decode("ascii"),
        },
    )

    assert response.status_code == 201
    assert response.get_json()["extraction"]["invoice_date"] is None
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).one().document_date == date(2026, 7, 7)


def test_receipt_ocr_api_suggest_and_book(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    client = _logged_in_client(app)

    pdf = _pdf_with_text(
        [
            "Muster Lieferant GmbH",
            "Rechnung Nr. 2026-4711",
            "Rechnungsdatum: 08.07.2026",
            "Nettobetrag 200,00 EUR",
            "MwSt 19 % 38,00 EUR",
            "Gesamtbetrag 238,00 EUR",
        ]
    )

    suggest = client.post(
        "/api/v1/receipt-ocr/suggestions",
        json={
            "company_id": company_id,
            "document_date": "2026-07-07",
            "file_name": "api-beleg.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf).decode("ascii"),
        },
    )
    assert suggest.status_code == 201
    suggestion = suggest.get_json()
    assert suggestion["extraction"]["supplier"] == "Muster Lieferant GmbH"
    assert suggestion["extraction"]["gross_amount"] == "238.00"
    document_id = suggestion["document_id"]

    with app.extensions["db_session_factory"]() as session:
        tax_code_id = session.query(TaxCode).one().id
        expense_id = session.query(Account).filter_by(code="6300").one().id
        creditor_id = session.query(Account).filter_by(code="1600").one().id

    book = client.post(
        "/api/v1/receipt-ocr/book",
        json={
            "company_id": company_id,
            "document_id": document_id,
            "expense_account_id": expense_id,
            "creditor_account_id": creditor_id,
            "tax_code_id": tax_code_id,
            "entry_date": "2026-07-08",
            "description": "Muster Lieferant 2026-4711",
            "net_amount": "200.00",
            "tax_amount": "38.00",
        },
    )
    assert book.status_code == 201
    assert book.get_json()["gross_amount"] == "238.00"

    with app.extensions["db_session_factory"]() as session:
        entry = session.query(JournalEntry).one()
        document = session.get(Document, document_id)
        assert document.journal_entry_id == entry.id
        assert document.document_date == entry.entry_date == date(2026, 7, 8)
        assert any(
            e.action == "ocr_booked"
            for e in session.query(AuditLog).filter_by(entity_type="document").all()
        )


def test_ocr_suggest_rejects_disallowed_type(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    client = _logged_in_client(app)

    response = client.post(
        "/belege/ocr/vorschlag",
        data={
            "company_id": str(company_id),
            "document_date": "2026-07-07",
            "document_file": (BytesIO(b"text"), "notiz.txt", "text/plain"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).count() == 0
