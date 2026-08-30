"""Buchungsvorlagen und Eröffnungsbilanz/Saldenübernahme."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.journal_templates import (
    JournalTemplateError,
    _add_months,
    book_template,
    create_template,
    due_templates,
)
from app.services.opening_balance import (
    OpeningBalanceError,
    book_opening_balance,
    parse_balance_csv,
)
from domain.models import (
    Account,
    Base,
    Company,
    JournalEntryLine,
    Tenant,
    User,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session):
    tenant = Tenant(name="Vorlagen Tenant")
    company = Company(tenant=tenant, name="Vorlagen GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts = {}
    for code, name, account_type in (
        ("1200", "Bank", "asset"),
        ("1400", "Forderungen", "asset"),
        ("1600", "Verbindlichkeiten", "liability"),
        ("4200", "Miete", "expense"),
        ("9000", "Saldenvorträge", "equity"),
    ):
        account = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
        )
        session.add(account)
        accounts[code] = account
    session.commit()
    return company, accounts


def _rent_lines(accounts) -> list[dict]:
    return [
        {"account_id": accounts["4200"].id, "debit": "595.00", "credit": "0"},
        {"account_id": accounts["1200"].id, "debit": "0", "credit": "595.00"},
    ]


def test_add_months_clamps_month_end() -> None:
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert _add_months(date(2026, 5, 15), 12) == date(2027, 5, 15)


def test_create_template_validates_lines(session: Session) -> None:
    company, accounts = _seed(session)

    with pytest.raises(JournalTemplateError, match="zwei Buchungszeilen"):
        create_template(
            session=session,
            company_id=company.id,
            name="Kaputt",
            description="",
            lines=[{"account_id": accounts["1200"].id, "debit": "10", "credit": "0"}],
            changed_by="t",
        )
    with pytest.raises(JournalTemplateError, match="Soll oder Haben"):
        create_template(
            session=session,
            company_id=company.id,
            name="Kaputt",
            description="",
            lines=[
                {"account_id": accounts["1200"].id, "debit": "10", "credit": "10"},
                {"account_id": accounts["4200"].id, "debit": "10", "credit": "0"},
            ],
            changed_by="t",
        )


def test_book_template_advances_next_run(session: Session) -> None:
    company, accounts = _seed(session)
    template = create_template(
        session=session,
        company_id=company.id,
        name="Miete Büro",
        description="Miete Büro monatlich",
        lines=_rent_lines(accounts),
        interval="monthly",
        next_run=date(2026, 8, 1),
        changed_by="tester",
    )
    assert due_templates(
        session=session, company_id=company.id, as_of=date(2026, 8, 15)
    ) == [template]

    entry, template = book_template(
        session=session, template_id=template.id, changed_by="tester"
    )
    assert entry.description == "Miete Büro monatlich"
    assert entry.entry_date == date(2026, 8, 1)
    assert template.next_run == date(2026, 9, 1)

    lines = session.execute(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
    ).scalars().all()
    assert len(lines) == 2

    # Deaktivierte Vorlage bucht nicht.
    template.is_active = False
    session.commit()
    with pytest.raises(JournalTemplateError, match="deaktiviert"):
        book_template(session=session, template_id=template.id, changed_by="tester")


def test_opening_balance_books_with_carryforward(session: Session) -> None:
    company, accounts = _seed(session)
    entry = book_opening_balance(
        session=session,
        company_id=company.id,
        entry_date=date(2026, 1, 1),
        balances=[
            {"account_code": "1200", "debit": "25000.00", "credit": "0"},
            {"account_code": "1400", "debit": "1190.00", "credit": "0"},
            {"account_code": "1600", "debit": "0", "credit": "595.00"},
        ],
        changed_by="tester",
    )
    lines = {
        row.account_id: (row.debit_amount, row.credit_amount)
        for row in session.execute(
            select(
                JournalEntryLine.account_id,
                JournalEntryLine.debit_amount,
                JournalEntryLine.credit_amount,
            ).where(JournalEntryLine.journal_entry_id == entry.id)
        )
    }
    # Differenz 25595,00 landet im Haben des Saldenvortragskontos.
    assert lines[accounts["9000"].id] == (Decimal("0.00"), Decimal("25595.00"))
    assert len(lines) == 4


def test_opening_balance_requires_carryforward_account_on_difference(
    session: Session,
) -> None:
    company, accounts = _seed(session)
    carryforward = accounts["9000"]
    carryforward.is_active = False
    session.commit()

    with pytest.raises(OpeningBalanceError, match="Saldenvortragskonto"):
        book_opening_balance(
            session=session,
            company_id=company.id,
            entry_date=date(2026, 1, 1),
            balances=[{"account_code": "1200", "debit": "100.00", "credit": "0"}],
            changed_by="tester",
        )


def test_parse_balance_csv_normalizes_amounts() -> None:
    balances = parse_balance_csv(
        "Konto;Soll;Haben\n1200;25.000,00;\n1600;;595,00\n\n1400;1190.00;0\n"
    )
    assert balances == [
        {"account_code": "1200", "debit": "25000.00", "credit": "0"},
        {"account_code": "1600", "debit": "0", "credit": "595.00"},
        {"account_code": "1400", "debit": "1190.00", "credit": "0.00"},
    ]
    with pytest.raises(OpeningBalanceError, match="Zeile 1"):
        parse_balance_csv("1200;abc;\n")


def test_templates_and_opening_balance_web_api_flow(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'tpl.db'}"}
    )
    with app.extensions["db_session_factory"]() as db_session:
        company, accounts = _seed(db_session)
        db_session.add(
            User(
                username="vorlagen",
                password_hash=hash_password("pw"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
        company_id = company.id
        rent_lines = _rent_lines(accounts)

    client = app.test_client()
    client.post("/auth/login", data={"username": "vorlagen", "password": "pw"})

    created = client.post(
        "/api/v1/journal-templates",
        json={
            "company_id": company_id,
            "name": "Miete",
            "description": "Miete monatlich",
            "interval": "monthly",
            "next_run": date.today().isoformat(),
            "lines": rent_lines,
        },
    )
    assert created.status_code == 201
    template_id = created.get_json()["id"]

    page = client.get(f"/buchungen?company_id={company_id}")
    assert "Buchungsvorlagen".encode() in page.data
    assert "fällig".encode() in page.data

    booked = client.post(f"/api/v1/journal-templates/{template_id}/book", json={})
    assert booked.status_code == 201
    assert booked.get_json()["template"]["next_run"] > date.today().isoformat()

    opening = client.post(
        "/api/v1/opening-balance",
        json={
            "company_id": company_id,
            "entry_date": date.today().isoformat(),
            "balances": [
                {"account_code": "1200", "debit": "1000.00", "credit": "0"},
            ],
        },
    )
    assert opening.status_code == 201

    ob_page = client.get(f"/eroeffnungsbilanz?company_id={company_id}")
    assert ob_page.status_code == 200
    assert "Saldenvortragskonto".encode() in ob_page.data
