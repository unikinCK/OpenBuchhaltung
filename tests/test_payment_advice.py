"""Zahlungsavise: Abschrift prüfen, offenen Posten zuordnen, Abzüge erklären, ausgleichen."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services import payment_advice as pa
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item
from app.services.partners import create_partner
from domain.models import (
    Account,
    BankTransaction,
    Base,
    Company,
    Document,
    JournalEntryLine,
    OpenItem,
    PaymentAdvice,
    TaxCode,
    Tenant,
    User,
)
from tests.test_receipt_matching import _pdf_with_text

AVIS_TEXT = """Zahlungsavis Nr. AV-77
Kunde AG
Zahldatum 20.07.2026
Beleg      Datum       Betrag     Skonto   Zahlbetrag
RE-1001    01.07.2026  1.190,00   23,80    1.166,20
RE-1002    05.07.2026  595,00     11,90    583,10
Bonus 2026                                 50,00-
Überweisung gesamt 1.699,30 EUR
"""


def _position(**fields) -> dict:
    position = {name: "" for name in pa._POSITION_FIELDS}
    position.update(fields)
    return position


def _page(positions: list[dict], **header) -> dict:
    page = {name: "" for name in pa._HEADER_FIELDS}
    page.update(header)
    page["abzuege_gesamt"] = header.pop("abzuege_gesamt", [])
    page["positionen"] = positions
    return page


AVIS_PAGE = _page(
    [
        _position(
            belegnr="RE-1001", datum="01.07.2026", rechnungsbetrag="1.190,00",
            skonto="23,80", zahlbetrag="1.166,20", art="rechnung",
        ),
        _position(
            belegnr="RE-1002", datum="05.07.2026", rechnungsbetrag="595,00",
            skonto="11,90", zahlbetrag="583,10", art="rechnung",
        ),
        _position(zahlbetrag="50,00-", art="abzug", text="Bonus 2026"),
    ],
    zahler="Kunde AG", avis_nummer="AV-77", zahldatum="20.07.2026", waehrung="EUR",
    zahlbetrag_gesamt="1.699,30",
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, tmp_path: Path, text: str = AVIS_TEXT) -> dict:
    tenant = Tenant(name="Avis Mandant")
    company = Company(tenant=tenant, name="Muster GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts = {}
    for code, name, account_type, subledger in (
        ("1200", "Bank", "asset", None),
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("1776", "Umsatzsteuer 19 %", "liability", None),
        ("8400", "Erlöse 19 % USt", "income", None),
        ("8736", "Gewährte Skonti 19 % USt", "income", None),
        ("8770", "Erlösschmälerungen", "income", None),
    ):
        account = Account(
            tenant_id=tenant.id, company_id=company.id, code=code, name=name,
            account_type=account_type, subledger=subledger,
        )
        session.add(account)
        accounts[code] = account
    session.flush()
    tax_code = TaxCode(
        tenant_id=tenant.id, company_id=company.id, code="USt19", kind="output",
        rate=Decimal("19.00"), vat_account_id=accounts["1776"].id,
    )
    session.add(tax_code)
    session.commit()
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde AG", is_customer=True
    )
    session.commit()
    items = []
    for reference, entry_date, amount in (
        ("RE-1001", date(2026, 7, 1), Decimal("1190.00")),
        ("RE-1002", date(2026, 7, 5), Decimal("595.00")),
    ):
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=entry_date,
                description=f"Ausgangsrechnung {reference}",
                status="posted",
                changed_by="t",
                lines=[
                    JournalLineInput(
                        accounts["1400"].id, amount, Decimal("0.00"), partner_id=customer.id
                    ),
                    JournalLineInput(accounts["8400"].id, Decimal("0.00"), amount),
                ],
            ),
        )
        items.append(
            create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company.id, account_id=accounts["1400"].id,
                    journal_entry_id=entry.id, item_type="receivable", reference=reference,
                    entry_date=entry_date, amount=amount, changed_by="t",
                    partner_id=customer.id,
                ),
            )
        )
    transaction = BankTransaction(
        tenant_id=tenant.id, company_id=company.id, bank_account_id=accounts["1200"].id,
        booking_date=date(2026, 7, 21), amount=Decimal("1699.30"), currency_code="EUR",
        purpose="Zahlung AV-77", counterparty="Kunde AG", dedup_hash="avis-1",
    )
    session.add(transaction)
    path = tmp_path / "avis.txt"
    path.write_text(text, encoding="utf-8")
    document = Document(
        tenant_id=tenant.id, company_id=company.id, file_name="avis.txt",
        storage_key=str(path), mime_type="text/plain", document_date=date(2026, 7, 21),
    )
    session.add(document)
    session.commit()
    return {
        "company": company, "accounts": accounts, "tax_code": tax_code, "partner": customer,
        "items": items, "transaction": transaction, "document": document,
    }


def _advice(session: Session, seed: dict, pages: list[dict], text: str = AVIS_TEXT):
    """Avis aus einer festen KI-Antwort je Seite auslesen und zuordnen."""
    answers = iter(pages)

    def extract(_text, *, feedback, company):
        return next(answers)

    advice = PaymentAdvice(
        tenant_id=seed["company"].tenant_id, company_id=seed["company"].id,
        document_id=seed["document"].id, status=pa.STATUS_READY, created_by="t",
    )
    session.add(advice)
    session.flush()
    advice.extraction = pa.extract_pages([text], extract=extract, company="Muster GmbH")
    pa.evaluate_advice(session, advice)
    session.commit()
    return advice


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.234,56", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("536,25", Decimal("536.25")),
        ("17,36-", Decimal("-17.36")),
        ("-17,36", Decimal("-17.36")),
        ("(17,36)", Decimal("-17.36")),
        ("1 234,50 EUR", Decimal("1234.50")),
        ("1.234", Decimal("1234.00")),
        ("", None),
        ("abc", None),
    ],
)
def test_parse_amount(raw, expected) -> None:
    assert pa.parse_amount(raw) == expected


def test_parse_date_and_categories() -> None:
    assert pa.parse_date("15.10.26") == date(2026, 10, 15)
    assert pa.parse_date("2026-10-15") == date(2026, 10, 15)
    assert pa.parse_date("15/10/2026") == date(2026, 10, 15)
    assert pa.parse_date("31.02.2026") is None
    assert pa.categorize("Konventionalstr. GS") == pa.CAT_PENALTY
    assert pa.categorize("Betaalkorting") == pa.CAT_SKONTO
    assert pa.categorize("DV 0,75 %") == pa.CAT_DELKREDERE
    assert pa.categorize("Delkredere-Gebühr") == pa.CAT_DELKREDERE
    assert pa.categorize("Ware beschädigt") == pa.CAT_CLAIM
    assert pa.categorize("Bearbeitungsgebuehr Zentralregulierung") == pa.CAT_FEE
    assert pa.categorize("irgendwas") == pa.CAT_UNCLEAR
    assert pa.skonto_rate(Decimal("1190.00"), Decimal("23.80")) == Decimal("2")
    assert pa.skonto_rate(Decimal("100.00"), Decimal("3.50")) is None  # mehrdeutig


def test_check_page_flags_values_not_on_page() -> None:
    page = pa._clean_page(
        _page([_position(belegnr="RE-1001", zahlbetrag="1.166,30", art="rechnung")])
    )
    problems = pa.check_page(page, AVIS_TEXT)
    assert any("1.166,30" in problem for problem in problems)
    assert pa.check_page(pa._clean_page(AVIS_PAGE), AVIS_TEXT) == []


def test_second_attempt_gets_feedback() -> None:
    wrong = _page([_position(belegnr="RE-1001", zahlbetrag="1.166,30", art="rechnung")])
    feedbacks = []
    answers = iter([wrong, AVIS_PAGE])

    def extract(_text, *, feedback, company):
        feedbacks.append(feedback)
        return next(answers)

    result = pa.extract_pages([AVIS_TEXT], extract=extract, company="Muster GmbH")
    assert feedbacks[0] == "" and "1.166,30" in feedbacks[1]
    assert result["pages"][0]["problems"] == []
    assert len(result["pages"][0]["data"]["positionen"]) == 3


def test_subtotal_lines_are_dropped() -> None:
    page = _page([*AVIS_PAGE["positionen"], _position(zahlbetrag="1.699,30", text="Summe")])
    page.update({k: AVIS_PAGE[k] for k in pa._HEADER_FIELDS})
    result = pa.extract_pages(
        [AVIS_TEXT], extract=lambda *_a, **_k: page, company="Muster GmbH"
    )
    assert len(result["pages"][0]["data"]["positionen"]) == 3


# ---------------------------------------------------------------------------
# Zuordnen und Ampel
# ---------------------------------------------------------------------------


def test_green_advice_matches_items_and_explains_deductions(session, tmp_path) -> None:
    seed = _seed(session, tmp_path)
    advice = _advice(session, seed, [AVIS_PAGE])

    assert advice.traffic_light == pa.GREEN, advice.result["reasons"]
    assert advice.total_amount == Decimal("1699.30")
    assert advice.payment_date == date(2026, 7, 20)
    assert advice.partner_id == seed["partner"].id
    lines = advice.result["lines"]
    assert [line["open_item_reference"] for line in lines[:2]] == ["RE-1001", "RE-1002"]
    assert lines[0]["components"] == [
        {"category": "skonto", "label": "Skonto", "amount": "23.80", "source": "Avis-Spalte"}
    ]
    assert lines[2]["components"][0]["category"] == pa.CAT_BONUS
    assert advice.result["checks"]["ok"] is True
    assert advice.result["booking"] == {
        "settled_total": "1785.00",
        "discount_total": "35.70",
        "other_deductions_total": "50.00",
        "by_category": [
            {"category": "bonus", "label": "Bonus/Konditionen", "amount": "50.00"},
            {"category": "skonto", "label": "Skonto", "amount": "35.70"},
        ],
    }
    assert advice.result["bank_candidates"][0]["id"] == seed["transaction"].id


def test_skonto_already_deducted_is_recognized(session, tmp_path) -> None:
    text = "Kunde AG\nRE-1001 01.07.2026 1.166,20\nGesamt 1.166,20\nZahldatum 10.07.2026"
    seed = _seed(session, tmp_path, text)
    page = _page(
        [_position(belegnr="RE-1001", datum="01.07.2026", zahlbetrag="1.166,20", art="rechnung")],
        zahlbetrag_gesamt="1.166,20", zahldatum="10.07.2026",
    )
    advice = _advice(session, seed, [page], text)
    line = advice.result["lines"][0]
    assert advice.traffic_light == pa.GREEN, advice.result["reasons"]
    assert line["components"][0]["amount"] == "23.80"
    assert "Skonto 2 %" in line["notes"][0]


def test_unclear_deduction_and_missing_reference_are_red(session, tmp_path) -> None:
    text = AVIS_TEXT.replace("Bonus 2026", "Abzug lt. Mail")
    seed = _seed(session, tmp_path, text)
    page = _page(
        [AVIS_PAGE["positionen"][0], _position(zahlbetrag="50,00-", art="abzug", text="Mail")],
        zahlbetrag_gesamt="1.699,30",
    )
    advice = _advice(session, seed, [page], text)
    assert advice.traffic_light == pa.RED
    texts = " ".join(reason["text"] for reason in advice.result["reasons"])
    assert "beim Kunden nachfragen" in texts
    assert "RE-1002 steht im Avis" in texts  # die Abschrift hat die Zeile vergessen
    assert advice.result["checks"]["ok"] is False

    with pytest.raises(pa.PaymentAdviceError, match="rot"):
        pa.apply_advice(
            session=session, advice=advice, bank_transaction_id=seed["transaction"].id,
            changed_by="t",
        )


def test_amount_match_without_number_is_yellow(session, tmp_path) -> None:
    text = "Kunde AG\nUnsere Nr. 884711 vom 05.07.2026 595,00\nGesamt 595,00"
    seed = _seed(session, tmp_path, text)
    page = _page(
        [_position(kunden_belegnr="884711", datum="05.07.2026", zahlbetrag="595,00",
                   art="rechnung")],
        zahler="Kunde AG", zahlbetrag_gesamt="595,00",
    )
    advice = _advice(session, seed, [page], text)
    assert advice.traffic_light == pa.YELLOW
    assert advice.result["lines"][0]["open_item_reference"] == "RE-1002"
    assert advice.result["lines"][0]["match"] == "amount"


def test_late_skonto_is_yellow(session, tmp_path) -> None:
    text = AVIS_TEXT.replace("20.07.2026", "20.09.2026")
    seed = _seed(session, tmp_path, text)
    page = dict(AVIS_PAGE, zahldatum="20.09.2026")
    advice = _advice(session, seed, [page], text)
    assert advice.traffic_light == pa.YELLOW
    assert "Skonto nach 81 Tagen" in " ".join(advice.result["lines"][0]["notes"])


# ---------------------------------------------------------------------------
# Ausgleich
# ---------------------------------------------------------------------------


def _apply(session, seed, advice):
    return pa.apply_advice(
        session=session,
        advice=advice,
        bank_transaction_id=seed["transaction"].id,
        changed_by="t",
        discount_account_id=seed["accounts"]["8736"].id,
        discount_tax_code_id=seed["tax_code"].id,
        deduction_account_id=seed["accounts"]["8770"].id,
    )


def test_apply_books_bank_items_skonto_with_vat_and_deductions(session, tmp_path) -> None:
    seed = _seed(session, tmp_path)
    advice = _apply(session, seed, _advice(session, seed, [AVIS_PAGE]))

    assert advice.status == pa.STATUS_APPLIED
    entry = advice.journal_entry
    lines = {
        (line.account.code, line.debit_amount, line.credit_amount)
        for line in session.execute(
            select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
        ).scalars()
    }
    zero = Decimal("0.00")
    assert lines == {
        ("1200", Decimal("1699.30"), zero),
        ("1400", zero, Decimal("1190.00")),
        ("1400", zero, Decimal("595.00")),
        ("8736", Decimal("30.00"), zero),
        ("1776", Decimal("5.70"), zero),
        ("8770", Decimal("50.00"), zero),
    }
    for item in seed["items"]:
        session.refresh(item)
        assert item.status == "settled" and item.open_amount == zero
    session.refresh(seed["transaction"])
    assert seed["transaction"].status == "booked"
    assert seed["transaction"].journal_entry_id == entry.id
    session.refresh(seed["document"])
    assert seed["document"].journal_entry_id == entry.id

    with pytest.raises(pa.PaymentAdviceError, match="bereits ausgeglichen"):
        _apply(session, seed, advice)
    with pytest.raises(pa.PaymentAdviceError, match="stornieren"):
        pa.delete_advice(session=session, advice=advice, changed_by="t")


def test_reversal_reopens_items_and_allows_delete(session, tmp_path) -> None:
    seed = _seed(session, tmp_path)
    advice = _apply(session, seed, _advice(session, seed, [AVIS_PAGE]))
    reverse_journal_entry(
        session=session, journal_entry_id=advice.journal_entry_id,
        reversal_date=date(2026, 7, 22), changed_by="t",
    )
    for item in seed["items"]:
        session.refresh(item)
        assert item.status == "open"
    session.refresh(seed["transaction"])
    assert seed["transaction"].status == "open"
    pa.delete_advice(session=session, advice=advice, changed_by="t")
    assert session.get(PaymentAdvice, advice.id) is None


def test_apply_requires_accounts_and_matching_transaction(session, tmp_path) -> None:
    seed = _seed(session, tmp_path)
    advice = _advice(session, seed, [AVIS_PAGE])
    with pytest.raises(pa.PaymentAdviceError, match="Skontokonto"):
        pa.apply_advice(
            session=session, advice=advice, bank_transaction_id=seed["transaction"].id,
            changed_by="t", deduction_account_id=seed["accounts"]["8770"].id,
        )
    other = BankTransaction(
        tenant_id=seed["company"].tenant_id, company_id=seed["company"].id,
        bank_account_id=seed["accounts"]["1200"].id, booking_date=date(2026, 7, 21),
        amount=Decimal("1700.00"), currency_code="EUR", purpose="x", dedup_hash="avis-2",
    )
    session.add(other)
    session.commit()
    with pytest.raises(pa.PaymentAdviceError, match="passt nicht"):
        pa.apply_advice(
            session=session, advice=advice, bank_transaction_id=other.id, changed_by="t",
            discount_account_id=seed["accounts"]["8736"].id,
            deduction_account_id=seed["accounts"]["8770"].id,
        )
    for item in seed["items"]:
        session.refresh(item)
        assert item.status == "open"
    assert session.get(OpenItem, seed["items"][0].id).open_amount == Decimal("1190.00")


# ---------------------------------------------------------------------------
# API und Oberfläche
# ---------------------------------------------------------------------------


def _app(tmp_path: Path, monkeypatch):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'avis.db'}",
            "RECEIPT_LLM_ENDPOINT_URL": "http://llm.invalid/v1/responses",
            "RECEIPT_OCR_ENDPOINT_URL": None,
        }
    )
    monkeypatch.setattr(pa, "_start_thread", lambda target: target())
    monkeypatch.setattr(pa, "call_extraction_llm", lambda *_a, **_k: AVIS_PAGE)
    with app.extensions["db_session_factory"]() as session:
        session.add(User(username="admin", password_hash=hash_password("admin123"), role="Admin"))
        session.commit()
        seed = _seed(session, tmp_path)
        ids = {
            "company_id": seed["company"].id,
            "document_id": seed["document"].id,
            "transaction_id": seed["transaction"].id,
            "skonto": seed["accounts"]["8736"].id,
            "tax_code": seed["tax_code"].id,
            "other": seed["accounts"]["8770"].id,
        }
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return app, client, ids


def test_api_flow_from_document_to_settlement(tmp_path, monkeypatch) -> None:
    _app_obj, client, ids = _app(tmp_path, monkeypatch)
    company_id = ids["company_id"]

    created = client.post(
        "/api/v1/payment-advices",
        json={"company_id": company_id, "document_id": ids["document_id"]},
    )
    assert created.status_code == 202, created.get_json()
    advice_id = created.get_json()["id"]

    again = client.post(
        "/api/v1/payment-advices",
        json={"company_id": company_id, "document_id": ids["document_id"]},
    )
    assert again.status_code == 422

    detail = client.get(f"/api/v1/payment-advices/{advice_id}?company_id={company_id}")
    body = detail.get_json()
    assert body["status"] == "ready" and body["traffic_light"] == "green"
    assert body["result"]["bank_candidates"][0]["id"] == ids["transaction_id"]

    listed = client.get(f"/api/v1/payment-advices?company_id={company_id}").get_json()
    assert listed["count"] == 1 and "result" not in listed["items"][0]

    assert client.post(
        f"/api/v1/payment-advices/{advice_id}/rematch", json={"company_id": company_id}
    ).status_code == 200

    applied = client.post(
        f"/api/v1/payment-advices/{advice_id}/apply",
        json={
            "company_id": company_id,
            "bank_transaction_id": ids["transaction_id"],
            "discount_account_id": ids["skonto"],
            "discount_tax_code_id": ids["tax_code"],
            "deduction_account_id": ids["other"],
        },
    )
    assert applied.status_code == 200, applied.get_json()
    assert applied.get_json()["status"] == "applied"
    assert applied.get_json()["posting_number"]

    deleted = client.post(
        f"/api/v1/payment-advices/{advice_id}/delete", json={"company_id": company_id}
    )
    assert deleted.status_code == 422


def test_failed_extraction_is_stored(tmp_path, monkeypatch) -> None:
    _app_obj, client, ids = _app(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise pa.ReceiptLLMError("KI-Endpoint ist nicht erreichbar.")

    monkeypatch.setattr(pa, "call_extraction_llm", broken)
    created = client.post(
        "/api/v1/payment-advices",
        json={"company_id": ids["company_id"], "document_id": ids["document_id"]},
    )
    advice_id = created.get_json()["id"]
    body = client.get(
        f"/api/v1/payment-advices/{advice_id}?company_id={ids['company_id']}"
    ).get_json()
    assert body["status"] == "failed"
    assert "nicht erreichbar" in body["error"]
    assert client.post(
        f"/api/v1/payment-advices/{advice_id}/delete", json={"company_id": ids["company_id"]}
    ).status_code == 200


def test_web_upload_detail_and_apply(tmp_path, monkeypatch) -> None:
    from io import BytesIO

    app, client, ids = _app(tmp_path, monkeypatch)
    company_id = ids["company_id"]
    page = client.get(f"/opos/avise?company_id={company_id}")
    assert page.status_code == 200
    assert "Avis auswerten" in page.get_data(as_text=True)

    with client.session_transaction() as flask_session:
        token = flask_session.get("_csrf_token")
    response = client.post(
        "/opos/avise",
        data={
            "_csrf_token": token or "",
            "company_id": str(company_id),
            "advice_file": (
                BytesIO(_pdf_with_text(AVIS_TEXT.replace("Ü", "U").splitlines())),
                "avis-upload.pdf",
            ),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302, response.get_data(as_text=True)
    with app.extensions["db_session_factory"]() as session:
        advice = session.execute(select(PaymentAdvice)).scalars().one()
        assert advice.document.file_name == "avis-upload.pdf"
        advice_id = advice.id

    detail = client.get(response.headers["Location"])
    html = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert "Ausgleichen und buchen" in html
    assert "RE-1001" in html

    applied = client.post(
        f"/opos/avise/{advice_id}/ausgleichen",
        data={
            "_csrf_token": token or "",
            "company_id": str(company_id),
            "bank_transaction_id": str(ids["transaction_id"]),
            "discount_account_id": str(ids["skonto"]),
            "discount_tax_code_id": str(ids["tax_code"]),
            "deduction_account_id": str(ids["other"]),
        },
    )
    assert applied.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        assert session.get(PaymentAdvice, advice_id).status == pa.STATUS_APPLIED


def test_mcp_tools_forward_arguments() -> None:
    from app.services.mcp_server import MCPServer
    from tests.test_mcp_server import RecordingHttp

    http = RecordingHttp()
    server = MCPServer(http=http)

    def call(name, arguments):
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return http.calls[-1]

    assert call("start_payment_advice", {"company_id": 7, "document_id": 3}) == (
        "POST", "/payment-advices", None, {"company_id": 7, "document_id": 3},
    )
    assert call("get_payment_advice", {"advice_id": 5, "company_id": 7}) == (
        "GET", "/payment-advices/5", {"company_id": 7}, None,
    )
    assert call(
        "apply_payment_advice",
        {"advice_id": 5, "company_id": 7, "bank_transaction_id": 9, "discount_account_id": 2},
    ) == (
        "POST",
        "/payment-advices/5/apply",
        None,
        {"company_id": 7, "bank_transaction_id": 9, "discount_account_id": 2},
    )
    assert call("delete_payment_advice", {"advice_id": 5, "company_id": 7}) == (
        "POST", "/payment-advices/5/delete", None, {"company_id": 7},
    )


def test_deduction_amount_in_other_column(session, tmp_path) -> None:
    """Das Modell schreibt einen Abzug auch in ``sonstiger_abzug`` statt in den Zahlbetrag."""
    seed = _seed(session, tmp_path)
    positions = [*AVIS_PAGE["positionen"][:2], _position(
        kunden_belegnr="BL-2026", sonstiger_abzug="50,00-", art="abzug", text="Bonus 2026"
    )]
    text = AVIS_TEXT.replace("Bonus 2026", "BL-2026 Bonus 2026")
    advice = _advice(session, seed, [dict(AVIS_PAGE, positionen=positions)], text)
    assert advice.traffic_light == pa.GREEN, advice.result["reasons"]
    assert advice.result["lines"][2]["components"][0]["amount"] == "50.00"
