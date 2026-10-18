"""Buchungsvorschlag je offenem Bankumsatz: Regel, Geldtransit, „wie zuletzt“."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.services.bank_import import book_transaction, import_bank_csv
from app.services.bank_proposals import booking_proposals
from app.services.bank_rules import create_rule
from domain.models import Account, BankTransaction, Base, TaxCode, User
from tests.test_bank_rules import _create_app, _seed


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _open(session, company_id):
    return (
        session.execute(
            select(BankTransaction)
            .where(BankTransaction.company_id == company_id, BankTransaction.status == "open")
            .order_by(BankTransaction.booking_date, BankTransaction.id)
        )
        .scalars()
        .all()
    )


def _tax_code(session, company, vat_account):
    tax_code = TaxCode(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="VSt19",
        kind="input",
        rate=Decimal("19.00"),
        vat_account_id=vat_account.id,
    )
    session.add(tax_code)
    session.commit()
    return tax_code


def test_history_proposal_takes_last_booking_of_same_counterparty(session):
    company, bank, telecom, rent = _seed(session)
    vat = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1576",
        name="Vorsteuer 19 %",
        account_type="asset",
    )
    session.add(vat)
    session.commit()
    tax_code = _tax_code(session, company, vat)
    telekom_july, rent_july, _telekom_extra = _open(session, company.id)
    book_transaction(
        session=session,
        transaction_id=telekom_july.id,
        contra_account_id=telecom.id,
        tax_code_id=tax_code.id,
        changed_by="tester",
    )

    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(
            "Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag\n"
            "05.08.2026;Telekom Rechnung August;TELEKOM DEUTSCHLAND ;-49,00\n"
            "06.08.2026;Gutschrift;Telekom Deutschland;12,00\n"
        ),
        changed_by="tester",
    )
    transactions = _open(session, company.id)
    proposals = booking_proposals(session=session, transactions=transactions)
    by_purpose = {t.purpose: proposals.get(t.id) for t in transactions}

    august = by_purpose["Telekom Rechnung August"]
    assert august is not None and august.source == "history"
    assert august.contra_account.id == telecom.id
    assert august.tax_code.id == tax_code.id  # Steuerzeile ist nicht das Gegenkonto
    assert august.history_transaction_id == telekom_july.id
    assert by_purpose["Telekom Sonderposten"].contra_account.id == telecom.id
    assert by_purpose["Gutschrift"] is None  # anderes Vorzeichen
    assert by_purpose["Miete Juli"] is None  # keine frühere Buchung
    assert rent_july.id not in proposals


def test_rule_wins_over_history(session):
    company, _bank, telecom, rent = _seed(session)
    telekom_july, *_ = _open(session, company.id)
    book_transaction(
        session=session,
        transaction_id=telekom_july.id,
        contra_account_id=rent.id,
        changed_by="tester",
    )
    create_rule(
        session=session,
        company_id=company.id,
        pattern="Telekom",
        contra_account_id=telecom.id,
        changed_by="tester",
    )
    session.commit()
    proposals = booking_proposals(session=session, transactions=_open(session, company.id))
    sonder = next(p for p in proposals.values() if p.source == "rule")
    assert sonder.contra_account.id == telecom.id
    assert sonder.rule_pattern == "Telekom"
    assert all(p.source == "rule" for p in proposals.values())


def test_api_and_bank_page_expose_proposal(tmp_path):
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        company, _bank, telecom, _rent = _seed(db_session)
        telekom_july, *_ = _open(db_session, company.id)
        book_transaction(
            session=db_session,
            transaction_id=telekom_july.id,
            contra_account_id=telecom.id,
            changed_by="tester",
        )
        db_session.add(
            User(username="vorschlag", password_hash=hash_password("pw"), role="Admin")
        )
        db_session.commit()
        company_id = company.id
        telecom_id = telecom.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "vorschlag", "password": "pw"})
    body = client.get(
        "/api/v1/bank-transactions",
        query_string={"company_id": company_id, "status": "open", "include_suggestions": "1"},
    ).get_json()
    proposals = {t["purpose"]: t["booking_proposal"] for t in body["transactions"]}
    assert proposals["Telekom Sonderposten"]["source"] == "history"
    assert proposals["Telekom Sonderposten"]["contra_account_id"] == telecom_id
    assert proposals["Telekom Sonderposten"]["contra_account_code"] == "4920"
    assert proposals["Miete Juli"] is None

    plain = client.get(
        "/api/v1/bank-transactions", query_string={"company_id": company_id}
    ).get_json()
    assert "booking_proposal" not in plain["transactions"][0]

    html = client.get(f"/bank?company_id={company_id}").get_data(as_text=True)
    assert "Wie zuletzt" in html
    assert f'<option value="{telecom_id}"\n                            selected>' in html
