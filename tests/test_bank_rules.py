"""Auto-Kontierungsregeln: Anlage, Matching und Regel-Lauf."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.bank_import import import_bank_csv
from app.services.bank_rules import (
    BankRuleError,
    apply_rules,
    create_rule,
    match_rules_for,
    set_rule_active,
)
from domain.models import Account, BankTransaction, Base, Company, JournalEntry, Tenant, User

CSV = """Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag
05.07.2026;Telekom Rechnung Juli;Telekom Deutschland;-49,00
06.07.2026;Miete Juli;Vermieter GmbH;-595,00
07.07.2026;Telekom Sonderposten;Telekom Deutschland;-10,00
"""


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session):
    tenant = Tenant(name="Regel Tenant")
    company = Company(tenant=tenant, name="Regel GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    telecom = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="4920",
        name="Telefon",
        account_type="expense",
    )
    rent = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="4200",
        name="Miete",
        account_type="expense",
    )
    session.add_all([bank, telecom, rent])
    session.commit()
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(CSV),
        changed_by="tester",
    )
    return company, bank, telecom, rent


def test_create_rule_validates_input(session: Session) -> None:
    company, bank, telecom, _ = _seed(session)

    with pytest.raises(BankRuleError, match="3 Zeichen"):
        create_rule(
            session=session,
            company_id=company.id,
            pattern="ab",
            contra_account_id=telecom.id,
            changed_by="tester",
        )
    with pytest.raises(BankRuleError, match="Gegenkonto"):
        create_rule(
            session=session,
            company_id=company.id,
            pattern="Telekom",
            contra_account_id=999,
            changed_by="tester",
        )

    rule = create_rule(
        session=session,
        company_id=company.id,
        pattern="  Telekom   Rechnung ",
        contra_account_id=telecom.id,
        changed_by="tester",
    )
    assert rule.pattern == "Telekom Rechnung"

    with pytest.raises(BankRuleError, match="existiert bereits"):
        create_rule(
            session=session,
            company_id=company.id,
            pattern="Telekom Rechnung",
            contra_account_id=telecom.id,
            changed_by="tester",
        )


def test_match_prefers_longest_pattern_and_skips_inactive(session: Session) -> None:
    company, bank, telecom, rent = _seed(session)
    generic = create_rule(
        session=session,
        company_id=company.id,
        pattern="Telekom",
        contra_account_id=rent.id,
        changed_by="tester",
    )
    specific = create_rule(
        session=session,
        company_id=company.id,
        pattern="Telekom Rechnung",
        contra_account_id=telecom.id,
        changed_by="tester",
    )

    transactions = session.execute(select(BankTransaction)).scalars().all()
    matches = match_rules_for(session=session, transactions=transactions)
    by_purpose = {t.purpose: matches.get(t.id) for t in transactions}
    assert by_purpose["Telekom Rechnung Juli"].id == specific.id
    assert by_purpose["Telekom Sonderposten"].id == generic.id
    assert by_purpose["Miete Juli"] is None

    set_rule_active(session=session, rule_id=generic.id, is_active=False, changed_by="t")
    matches = match_rules_for(session=session, transactions=transactions)
    assert session.get(BankTransaction, transactions[2].id).purpose == "Telekom Sonderposten"
    assert transactions[2].id not in matches or matches[
        transactions[2].id
    ].id == specific.id


def test_apply_rules_books_matching_transactions(session: Session) -> None:
    company, bank, telecom, _ = _seed(session)
    create_rule(
        session=session,
        company_id=company.id,
        pattern="Telekom",
        contra_account_id=telecom.id,
        changed_by="tester",
    )

    report = apply_rules(session=session, company_id=company.id, changed_by="tester")
    assert report.matched == 2
    assert report.booked == 2
    assert report.errors == []

    booked = (
        session.execute(select(BankTransaction).where(BankTransaction.status == "booked"))
        .scalars()
        .all()
    )
    assert len(booked) == 2
    entry = session.get(JournalEntry, booked[0].journal_entry_id)
    assert "Regel: Telekom" in entry.description

    # Zweiter Lauf findet nichts mehr.
    second = apply_rules(session=session, company_id=company.id, changed_by="tester")
    assert second.matched == 0


def _create_app(tmp_path: Path):
    return create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'rules.db'}"}
    )


def test_rules_api_roundtrip(tmp_path):
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        company, bank, telecom, _ = _seed(db_session)
        db_session.add(
            User(
                username="regler",
                password_hash=hash_password("pw"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
        company_id, telecom_id = company.id, telecom.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "regler", "password": "pw"})

    created = client.post(
        "/api/v1/bank-booking-rules",
        json={
            "company_id": company_id,
            "pattern": "Telekom",
            "contra_account_id": telecom_id,
        },
    )
    assert created.status_code == 201
    rule_id = created.get_json()["id"]

    listing = client.get(f"/api/v1/bank-booking-rules?company_id={company_id}")
    assert [r["id"] for r in listing.get_json()["rules"]] == [rule_id]

    applied = client.post(
        "/api/v1/bank-booking-rules/apply", json={"company_id": company_id}
    )
    assert applied.status_code == 200
    body = applied.get_json()
    assert body["matched"] == 2
    assert body["booked"] == 2

    deactivated = client.post(
        f"/api/v1/bank-booking-rules/{rule_id}/active", json={"is_active": False}
    )
    assert deactivated.status_code == 200
    assert deactivated.get_json()["is_active"] is False
    assert client.get(
        f"/api/v1/bank-booking-rules?company_id={company_id}"
    ).get_json()["rules"] == []


def test_bank_page_shows_rule_suggestion(tmp_path):
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        company, bank, telecom, _ = _seed(db_session)
        create_rule(
            session=db_session,
            company_id=company.id,
            pattern="Telekom",
            contra_account_id=telecom.id,
            changed_by="tester",
        )
        db_session.add(
            User(
                username="regler",
                password_hash=hash_password("pw"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
        company_id = company.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "regler", "password": "pw"})
    page = client.get(f"/bank?company_id={company_id}")
    assert page.status_code == 200
    html = page.data.decode()
    assert "Auto-Kontierung (Regeln)" in html
    assert "Regel</span>" in html
    assert "Regel-Treffer verbuchen" in html
