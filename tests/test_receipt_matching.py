"""Tests für den Belegabgleich: Vorschläge (Match/Neue Buchung), Freigabe, Ablehnung."""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.error import URLError

import pytest
from document_files import MIN_DOCUMENT_BYTES

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services import receipt_matching, receipt_ocr
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.receipt_matching import (
    ReceiptMatchError,
    create_match_suggestion,
    find_candidate_entries,
)
from domain.models import (
    Account,
    AuditLog,
    Company,
    Document,
    JournalEntry,
    ReceiptMatchSuggestion,
    TaxCode,
    Tenant,
    User,
)

RECEIPT_LINES = [
    "Muster Lieferant GmbH",
    "Rechnung Nr. 2026-4711",
    "Rechnungsdatum: 08.07.2026",
    "Nettobetrag 200,00 EUR",
    "MwSt 19 % 38,00 EUR",
    "Gesamtbetrag 238,00 EUR",
]


def _pdf_with_text(lines: list[str]) -> bytes:
    """Minimales PDF mit Textebene, aufgefüllt auf die Upload-Mindestgröße."""
    shows = " ".join(f"({line}) Tj 0 -14 Td" for line in lines)
    content = f"BT /F1 12 Tf 50 800 Td {shows} ET".encode("latin-1")
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length " + str(len(content)).encode("ascii") + b">>\nstream\n"
        + content
        + b"\nendstream endobj\n"
    )
    padding = max(0, MIN_DOCUMENT_BYTES - len(pdf) - len(b"%\n%%EOF"))
    return pdf + b"%" + b"0" * padding + b"\n%%EOF"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _create_test_app(tmp_path: Path, **extra_config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'match_app.db'}",
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


def _logged_in_client(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return client


def _seed_company_with_accounts(
    app, *, tenant_name="Abgleich-Mandant", company_name="Abgleich GmbH"
):
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name=tenant_name)
        session.add(tenant)
        session.flush()
        company = Company(tenant_id=tenant.id, name=company_name)
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


def _account_id(app, company_id: int, code: str) -> int:
    with app.extensions["db_session_factory"]() as session:
        return (
            session.query(Account)
            .filter(Account.company_id == company_id, Account.code == code)
            .one()
            .id
        )


def _tax_code_id(app, company_id: int) -> int:
    with app.extensions["db_session_factory"]() as session:
        return session.query(TaxCode).filter(TaxCode.company_id == company_id).one().id


def _upload_document(app, company_id: int, lines: list[str], file_name: str = "beleg.pdf") -> int:
    pdf = _pdf_with_text(lines)
    with app.extensions["db_session_factory"]() as session:
        company = session.get(Company, company_id)
        upload_dir = (
            Path(app.config["DOCUMENT_UPLOAD_DIR"]) / str(company.tenant_id) / str(company.id)
        )
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / file_name
        target.write_bytes(pdf)
        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name=file_name,
            storage_key=str(target),
            mime_type="application/pdf",
            document_date=date(2026, 7, 8),
        )
        session.add(document)
        session.commit()
        return document.id


def _create_entry(
    app,
    company_id: int,
    gross: str,
    entry_date: date,
    description: str,
) -> int:
    with app.extensions["db_session_factory"]() as session:
        expense_id = (
            session.query(Account)
            .filter(Account.company_id == company_id, Account.code == "6300")
            .one()
            .id
        )
        creditor_id = (
            session.query(Account)
            .filter(Account.company_id == company_id, Account.code == "1600")
            .one()
            .id
        )
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company_id,
                entry_date=entry_date,
                description=description,
                status="posted",
                changed_by="test",
                lines=[
                    JournalLineInput(
                        account_id=expense_id,
                        debit_amount=Decimal(gross),
                        credit_amount=Decimal("0.00"),
                    ),
                    JournalLineInput(
                        account_id=creditor_id,
                        debit_amount=Decimal("0.00"),
                        credit_amount=Decimal(gross),
                    ),
                ],
            ),
        )
        return entry.id


# ---------------------------------------------------------------------------
# Kandidatensuche (regelbasiert)
# ---------------------------------------------------------------------------


def test_find_candidates_skips_linked_and_reversal_entries(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_a = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Kandidat A")
    entry_b = _create_entry(app, company_id, "238.00", date(2026, 7, 12), "Kandidat B")

    with app.extensions["db_session_factory"]() as session:
        # Entry A bereits mit einem Beleg verknüpft -> kein Kandidat mehr.
        company = session.get(Company, company_id)
        session.add(
            Document(
                tenant_id=company.tenant_id,
                company_id=company.id,
                file_name="alt.pdf",
                storage_key=str(tmp_path / "alt.pdf"),
                mime_type="application/pdf",
                journal_entry_id=entry_a,
            )
        )
        session.commit()

        candidates = find_candidate_entries(
            session=session, company_id=company_id, gross_amount=Decimal("238.00")
        )
        assert [entry.id for entry in candidates] == [entry_b]

        no_amount_ids = [
            entry.id
            for entry in find_candidate_entries(
                session=session, company_id=company_id, gross_amount=None
            )
        ]
        assert entry_b in no_amount_ids
        assert entry_a not in no_amount_ids


# ---------------------------------------------------------------------------
# API: Match-Vorschlag erzeugen, freigeben (auch mit Änderung), ablehnen
# ---------------------------------------------------------------------------


def test_rule_based_match_suggest_and_approve_via_api(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Muster Lieferant")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    created = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["suggestion_type"] == "match"
    assert body["journal_entry_id"] == entry_id
    assert body["status"] == "offen"
    assert body["llm_used"] is False
    assert body["gross_amount"] == "238.00"
    assert body["journal_entry"]["posting_number"]

    listed = client.get(
        f"/api/v1/receipt-matching/suggestions?company_id={company_id}&status=offen"
    )
    assert listed.status_code == 200
    assert listed.get_json()["count"] == 1

    approved = client.post(
        f"/api/v1/receipt-matching/suggestions/{body['id']}/approve",
        json={"company_id": company_id},
    )
    assert approved.status_code == 200
    assert approved.get_json()["status"] == "freigegeben"

    with app.extensions["db_session_factory"]() as session:
        document = session.get(Document, document_id)
        assert document.journal_entry_id == entry_id
        actions = [
            row.action
            for row in session.query(AuditLog)
            .filter(AuditLog.entity_type == "receipt_match_suggestion")
            .all()
        ]
        assert actions == ["created", "approved"]


def test_match_approve_with_override_via_api(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    near_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Nahe Buchung")
    far_id = _create_entry(app, company_id, "238.00", date(2026, 9, 20), "Ferne Buchung")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    created = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    body = created.get_json()
    # Mehrdeutig: Vorschlag wählt die datumsnächste Buchung mit niedriger Konfidenz.
    assert body["journal_entry_id"] == near_id
    assert body["confidence"] == "niedrig"

    approved = client.post(
        f"/api/v1/receipt-matching/suggestions/{body['id']}/approve",
        json={"company_id": company_id, "journal_entry_id": far_id},
    )
    assert approved.status_code == 200
    assert approved.get_json()["journal_entry_id"] == far_id

    with app.extensions["db_session_factory"]() as session:
        assert session.get(Document, document_id).journal_entry_id == far_id
        approved_log = (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_type == "receipt_match_suggestion",
                AuditLog.action == "approved",
            )
            .one()
        )
        assert approved_log.payload["overridden"] is True
        assert approved_log.payload["suggested_journal_entry_id"] == near_id


def test_new_booking_suggestion_and_booking_via_api(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    created = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["suggestion_type"] == "new_booking"
    assert body["journal_entry_id"] is None
    assert body["net_amount"] == "200.00"
    assert body["tax_amount"] == "38.00"
    assert body["supplier"] == "Muster Lieferant GmbH"

    approved = client.post(
        f"/api/v1/receipt-matching/suggestions/{body['id']}/approve",
        json={
            "company_id": company_id,
            "expense_account_id": _account_id(app, company_id, "6300"),
            "creditor_account_id": _account_id(app, company_id, "1600"),
            "tax_code_id": _tax_code_id(app, company_id),
            "entry_date": "2026-07-08",
            "net_amount": "200.00",
            "tax_amount": "38.00",
        },
    )
    assert approved.status_code == 200
    approved_body = approved.get_json()
    assert approved_body["status"] == "freigegeben"
    assert approved_body["journal_entry"]["posting_number"]

    with app.extensions["db_session_factory"]() as session:
        document = session.get(Document, document_id)
        entry = session.get(JournalEntry, approved_body["journal_entry_id"])
        assert document.journal_entry_id == entry.id
        assert len(entry.lines) == 3
        assert sum(line.debit_amount for line in entry.lines) == Decimal("238.00")
        assert entry.entry_date == date(2026, 7, 8)


def test_reject_suggestion_and_retry_via_api(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    created = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    suggestion_id = created.get_json()["id"]

    duplicate = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert duplicate.status_code == 422
    assert "offener Abgleichsvorschlag" in duplicate.get_json()["error"]

    rejected = client.post(
        f"/api/v1/receipt-matching/suggestions/{suggestion_id}/reject",
        json={"company_id": company_id},
    )
    assert rejected.status_code == 200
    assert rejected.get_json()["status"] == "abgelehnt"

    with app.extensions["db_session_factory"]() as session:
        assert session.get(Document, document_id).journal_entry_id is None

    # Nach der Ablehnung ist ein erneuter Abgleich möglich.
    retry = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert retry.status_code == 201

    # Bereits entschiedene Vorschläge können nicht erneut entschieden werden.
    again = client.post(
        f"/api/v1/receipt-matching/suggestions/{suggestion_id}/reject",
        json={"company_id": company_id},
    )
    assert again.status_code == 422


def test_linked_document_cannot_be_matched(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Verknüpft")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    with app.extensions["db_session_factory"]() as session:
        session.get(Document, document_id).journal_entry_id = entry_id
        session.commit()

    client = _logged_in_client(app)
    response = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert response.status_code == 422
    assert "bereits mit einer Buchung verknüpft" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# LLM-Entscheidung und Fallback
# ---------------------------------------------------------------------------


def test_llm_picks_match_from_candidates(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Andere Rechnung")
    target_id = _create_entry(app, company_id, "238.00", date(2026, 7, 12), "Muster Lieferant")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)

    captured = {}

    def match_urlopen(request, timeout=0):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "output_text": json.dumps(
                    {
                        "journal_entry_id": target_id,
                        "confidence": "hoch",
                        "reason": "Betrag und Lieferant passen zur Buchung.",
                    }
                )
            }
        )

    monkeypatch.setattr(receipt_matching, "urlopen", match_urlopen)
    # Extraktions-Kontroll-LLM bestätigt den regelbasierten Bruttobetrag.
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(
            {"output_text": json.dumps({"gross_amount": 238.0})}
        ),
    )

    with app.extensions["db_session_factory"]() as session:
        suggestion = create_match_suggestion(
            session=session,
            company_id=company_id,
            document_id=document_id,
            changed_by="test",
            llm_endpoint="https://llm.example/responses",
            llm_model="test-model",
        )
        assert suggestion.suggestion_type == "match"
        assert suggestion.journal_entry_id == target_id
        assert suggestion.confidence == "hoch"
        assert suggestion.llm_used is True
        assert "Lieferant passen" in suggestion.reason

    # Der Prompt enthält Belegdaten und beide Kandidaten.
    user_text = captured["payload"]["input"][1]["content"][0]["text"]
    assert "Muster Lieferant GmbH" in user_text
    assert str(target_id) in user_text


def test_llm_failure_falls_back_to_rule_based(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Muster Lieferant")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)

    def broken_urlopen(request, timeout=0):
        raise URLError("down")

    monkeypatch.setattr(receipt_matching, "urlopen", broken_urlopen)
    monkeypatch.setattr(receipt_ocr, "urlopen", broken_urlopen)

    with app.extensions["db_session_factory"]() as session:
        suggestion = create_match_suggestion(
            session=session,
            company_id=company_id,
            document_id=document_id,
            changed_by="test",
            llm_endpoint="https://llm.example/responses",
            llm_model="test-model",
        )
        assert suggestion.suggestion_type == "match"
        assert suggestion.journal_entry_id == entry_id
        assert suggestion.llm_used is False
        assert "LLM-Abgleich nicht möglich" in suggestion.reason


def test_llm_hallucinated_entry_id_is_rejected(tmp_path, monkeypatch):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Muster Lieferant")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)

    monkeypatch.setattr(
        receipt_matching,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(
            {
                "output_text": json.dumps(
                    {"journal_entry_id": 999999, "confidence": "hoch", "reason": "?"}
                )
            }
        ),
    )
    monkeypatch.setattr(
        receipt_ocr,
        "urlopen",
        lambda request, timeout=0: _FakeResponse(
            {"output_text": json.dumps({"gross_amount": 238.0})}
        ),
    )

    with app.extensions["db_session_factory"]() as session:
        suggestion = create_match_suggestion(
            session=session,
            company_id=company_id,
            document_id=document_id,
            changed_by="test",
            llm_endpoint="https://llm.example/responses",
            llm_model="test-model",
        )
        # Halluzinierte ID -> regelbasierter Fallback auf den echten Kandidaten.
        assert suggestion.journal_entry_id == entry_id
        assert suggestion.llm_used is False
        assert "außerhalb der Kandidaten" in suggestion.reason


# ---------------------------------------------------------------------------
# UI-Flow
# ---------------------------------------------------------------------------


def test_ui_flow_suggest_and_approve(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    entry_id = _create_entry(app, company_id, "238.00", date(2026, 7, 10), "Muster Lieferant")
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    page = client.get(f"/belege/abgleich?company_id={company_id}")
    assert page.status_code == 200
    assert "Belegabgleich" in page.get_data(as_text=True)
    assert "Abgleich starten" in page.get_data(as_text=True)

    suggest = client.post(
        "/belege/abgleich/vorschlag",
        data={"company_id": company_id, "document_id": document_id},
    )
    assert suggest.status_code == 302

    page = client.get(f"/belege/abgleich?company_id={company_id}")
    html = page.get_data(as_text=True)
    assert "Offene Vorschläge" in html
    assert "Freigeben und verknüpfen" in html

    with app.extensions["db_session_factory"]() as session:
        suggestion = session.query(ReceiptMatchSuggestion).one()
        suggestion_id = suggestion.id
        assert suggestion.journal_entry_id == entry_id

    approve = client.post(
        f"/belege/abgleich/{suggestion_id}/freigeben",
        data={"company_id": company_id, "journal_entry_id": entry_id},
    )
    assert approve.status_code == 302

    with app.extensions["db_session_factory"]() as session:
        assert session.get(Document, document_id).journal_entry_id == entry_id

    page = client.get(f"/belege/abgleich?company_id={company_id}")
    assert "freigegeben" in page.get_data(as_text=True)


def test_ui_new_booking_form_books_suggestion(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    client = _logged_in_client(app)

    client.post(
        "/belege/abgleich/vorschlag",
        data={"company_id": company_id, "document_id": document_id},
    )
    with app.extensions["db_session_factory"]() as session:
        suggestion = session.query(ReceiptMatchSuggestion).one()
        assert suggestion.suggestion_type == "new_booking"
        suggestion_id = suggestion.id

    page = client.get(f"/belege/abgleich?company_id={company_id}")
    assert "Buchung anlegen und freigeben" in page.get_data(as_text=True)

    book = client.post(
        f"/belege/abgleich/{suggestion_id}/buchen",
        data={
            "company_id": company_id,
            "expense_account_id": _account_id(app, company_id, "6300"),
            "creditor_account_id": _account_id(app, company_id, "1600"),
            "tax_code_id": _tax_code_id(app, company_id),
            "entry_date": "2026-07-08",
            "net_amount": "200.00",
            "tax_amount": "38.00",
            "description": "Muster Lieferant 2026-4711",
        },
    )
    assert book.status_code == 302

    with app.extensions["db_session_factory"]() as session:
        suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
        assert suggestion.status == "freigegeben"
        document = session.get(Document, document_id)
        assert document.journal_entry_id == suggestion.journal_entry_id
        entry = session.get(JournalEntry, suggestion.journal_entry_id)
        assert entry.description == "Muster Lieferant 2026-4711"


# ---------------------------------------------------------------------------
# Rollen und Tenant-Scoping
# ---------------------------------------------------------------------------


def test_roles_and_tenant_scoping(tmp_path):
    buchhalter_token = "obk_match-buchhalter"
    pruefer_token = "obk_match-pruefer"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    company_a_id = _seed_company_with_accounts(app, tenant_name="Mandant A", company_name="A GmbH")
    company_b_id = _seed_company_with_accounts(app, tenant_name="Mandant B", company_name="B GmbH")
    document_b_id = _upload_document(app, company_b_id, RECEIPT_LINES, file_name="fremd.pdf")

    with app.extensions["db_session_factory"]() as session:
        tenant_a_id = session.get(Company, company_a_id).tenant_id
        session.add_all(
            [
                User(
                    username="buchhalter-a",
                    password_hash=hash_password("pw"),
                    role="Buchhalter",
                    tenant_id=tenant_a_id,
                    api_token_hash=hash_api_token(buchhalter_token),
                    api_token_last4=buchhalter_token[-4:],
                ),
                User(
                    username="pruefer-a",
                    password_hash=hash_password("pw"),
                    role="Pruefer",
                    tenant_id=tenant_a_id,
                    api_token_hash=hash_api_token(pruefer_token),
                    api_token_last4=pruefer_token[-4:],
                ),
            ]
        )
        session.commit()
        # Offener Vorschlag im fremden Mandanten B.
        foreign_suggestion = create_match_suggestion(
            session=session,
            company_id=company_b_id,
            document_id=document_b_id,
            changed_by="test",
        )
        foreign_suggestion_id = foreign_suggestion.id

    client = app.test_client()
    pruefer_write = client.post(
        "/api/v1/receipt-matching/suggestions",
        headers={"Authorization": f"Bearer {pruefer_token}"},
        json={"company_id": company_a_id, "document_id": 1},
    )
    assert pruefer_write.status_code == 403

    cross_create = client.post(
        "/api/v1/receipt-matching/suggestions",
        headers={"Authorization": f"Bearer {buchhalter_token}"},
        json={"company_id": company_b_id, "document_id": document_b_id},
    )
    assert cross_create.status_code == 404

    cross_list = client.get(
        f"/api/v1/receipt-matching/suggestions?company_id={company_b_id}",
        headers={"Authorization": f"Bearer {buchhalter_token}"},
    )
    assert cross_list.status_code == 404

    cross_reject = client.post(
        f"/api/v1/receipt-matching/suggestions/{foreign_suggestion_id}/reject",
        headers={"Authorization": f"Bearer {buchhalter_token}"},
        json={"company_id": company_b_id},
    )
    assert cross_reject.status_code == 404


def test_suggestion_fields_are_sanitized_before_persisting(tmp_path, monkeypatch):
    """NUL-Bytes/Steuerzeichen aus der Extraktion dürfen nie in die DB gelangen.

    PostgreSQL wirft bei NUL-Bytes in Textspalten einen DataError; die
    Persistierung muss unabhängig von der OCR-Stufe bereinigen.
    """
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    document_id = _upload_document(app, company_id, RECEIPT_LINES)

    def fake_analyze(**kwargs):
        return receipt_ocr.ReceiptExtraction(
            raw_text="egal",
            supplier="\x00A\x00m\x00a\x00z\x00o\x00n",
            invoice_number="R-\x0047\x0011",
            gross_amount=Decimal("238.00"),
        )

    monkeypatch.setattr(receipt_matching, "analyze_document", fake_analyze)

    with app.extensions["db_session_factory"]() as session:
        suggestion = create_match_suggestion(
            session=session,
            company_id=company_id,
            document_id=document_id,
            changed_by="test",
        )
        assert suggestion.supplier == "Amazon"
        assert suggestion.invoice_number == "R-4711"
        assert "\x00" not in suggestion.reason
        assert suggestion.currency_code == "EUR"


def test_garbled_pdf_yields_clear_error_instead_of_500(tmp_path, monkeypatch):
    """Zeichensalat aus der PDF-Extraktion muss als ReceiptMatchError ankommen."""
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    document_id = _upload_document(app, company_id, RECEIPT_LINES)
    # Bordmittel- und pypdf-Extraktion liefern nur NUL-durchsetzten Müll.
    monkeypatch.setattr(
        receipt_ocr,
        "_extract_pdf_text",
        lambda pdf_bytes: "\x005\x00H\x00F\x00K\x00Q\x00X\x00Q\x00J" * 5,
    )
    client = _logged_in_client(app)

    response = client.post(
        "/api/v1/receipt-matching/suggestions",
        json={"company_id": company_id, "document_id": document_id},
    )
    assert response.status_code == 422
    assert "Textebene" in response.get_json()["error"]


def test_missing_file_yields_clear_error(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _seed_company_with_accounts(app)
    with app.extensions["db_session_factory"]() as session:
        company = session.get(Company, company_id)
        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name="verschwunden.pdf",
            storage_key=str(tmp_path / "gibt-es-nicht.pdf"),
            mime_type="application/pdf",
        )
        session.add(document)
        session.commit()
        document_id = document.id

        with pytest.raises(ReceiptMatchError, match="nicht lesbar"):
            create_match_suggestion(
                session=session,
                company_id=company_id,
                document_id=document_id,
                changed_by="test",
            )
