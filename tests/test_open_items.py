from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    settle_open_item,
)
from domain.models import Account, AuditLog, BankTransaction, Base, Company, OpenItem, Tenant, User


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account, Account, Account]:
    tenant = Tenant(name="OPOS Tenant")
    company = Company(tenant=tenant, name="OPOS GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    receivable = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1400",
        name="Forderungen",
        account_type="receivable",
    )
    revenue = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="income",
    )
    session.add_all([bank, receivable, revenue])
    session.commit()
    return company, bank, receivable, revenue


def test_create_and_settle_open_item_with_bank_transactions(session: Session) -> None:
    company, bank, receivable, revenue = _seed_company(session)
    invoice = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 1),
            description="Ausgangsrechnung RE-1",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(receivable.id, Decimal("1190.00"), Decimal("0.00")),
                JournalLineInput(revenue.id, Decimal("0.00"), Decimal("1190.00")),
            ],
        ),
    )

    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=receivable.id,
            journal_entry_id=invoice.id,
            item_type="receivable",
            reference="RE-1",
            counterparty="Kunde AG",
            entry_date=date(2026, 7, 1),
            due_date=date(2026, 7, 15),
            amount=Decimal("1190.00"),
            changed_by="pytest",
        ),
    )
    assert item.status == "open"
    assert item.open_amount == Decimal("1190.00")

    first_payment = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank.id,
        booking_date=date(2026, 7, 5),
        amount=Decimal("500.00"),
        currency_code="EUR",
        purpose="Teilzahlung RE-1",
        counterparty="Kunde AG",
        dedup_hash="opos-1",
    )
    second_payment = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank.id,
        booking_date=date(2026, 7, 8),
        amount=Decimal("690.00"),
        currency_code="EUR",
        purpose="Restzahlung RE-1",
        counterparty="Kunde AG",
        dedup_hash="opos-2",
    )
    session.add_all([first_payment, second_payment])
    session.commit()

    partial = settle_open_item(
        session=session,
        open_item_id=item.id,
        bank_transaction_id=first_payment.id,
        journal_entry_id=invoice.id,
        changed_by="pytest",
    )
    assert partial.status == "open"
    assert partial.open_amount == Decimal("690.00")
    assert session.get(BankTransaction, first_payment.id).status == "matched"

    settled = settle_open_item(
        session=session,
        open_item_id=item.id,
        bank_transaction_id=second_payment.id,
        journal_entry_id=invoice.id,
        changed_by="pytest",
    )
    assert settled.status == "settled"
    assert settled.open_amount == Decimal("0.00")
    assert settled.settled_by == "pytest"

    actions = session.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "open_item")
    ).scalars().all()
    assert actions == ["created", "partially_settled", "settled"]


def test_receivable_rejects_outgoing_bank_transaction(session: Session) -> None:
    company, bank, receivable, _ = _seed_company(session)
    item = create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=receivable.id,
            item_type="receivable",
            reference="RE-neg",
            entry_date=date(2026, 7, 1),
            amount=Decimal("100.00"),
            changed_by="pytest",
        ),
    )
    outgoing = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank.id,
        booking_date=date(2026, 7, 5),
        amount=Decimal("-100.00"),
        currency_code="EUR",
        purpose="falsche Richtung",
        dedup_hash="opos-neg",
    )
    session.add(outgoing)
    session.commit()

    with pytest.raises(OpenItemError, match="Zahlungseing"):
        settle_open_item(
            session=session,
            open_item_id=item.id,
            bank_transaction_id=outgoing.id,
            changed_by="pytest",
        )


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_open_items.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    return app


def test_open_items_ui_create_and_settle(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "OPOS UI", "company_name": "OPOS UI GmbH"})
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1400",
            "name": "Forderungen",
            "account_type": "receivable",
        },
    )

    page = client.get("/offene-posten?company_id=1")
    assert page.status_code == 200
    assert b"Offene Posten" in page.data

    create_response = client.post(
        "/offene-posten",
        data={
            "company_id": "1",
            "account_id": "1",
            "item_type": "receivable",
            "reference": "RE-UI-1",
            "counterparty": "Kunde UI",
            "entry_date": "2026-07-01",
            "due_date": "2026-07-15",
            "amount": "1190.00",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert b"RE-UI-1" in create_response.data
    assert b"1190.00" in create_response.data

    settle_response = client.post(
        "/offene-posten/1/ausgleichen",
        data={"company_id": "1"},
        follow_redirects=True,
    )
    assert settle_response.status_code == 200
    assert b"wurde ausgeglichen" in settle_response.data

    with app.extensions["db_session_factory"]() as session:
        item = session.get(OpenItem, 1)
        assert item.status == "settled"
        assert item.open_amount == Decimal("0.00")
