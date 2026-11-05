from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.bank_import import book_transaction
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    settle_open_item,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    OpenItem,
    OpenItemSettlement,
    Tenant,
    User,
)


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
        account_type="asset",
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


def _receivable(session: Session, company: Company, account: Account, reference: str) -> OpenItem:
    return create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=account.id,
            item_type="receivable",
            reference=reference,
            entry_date=date(2026, 7, 1),
            amount=Decimal("100.00"),
            changed_by="pytest",
        ),
    )


def _incoming(session: Session, company: Company, bank: Account, amount: str) -> BankTransaction:
    transaction = BankTransaction(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank.id,
        booking_date=date(2026, 7, 5),
        amount=Decimal(amount),
        currency_code="EUR",
        purpose="Zahlung",
        dedup_hash=f"opos-cap-{amount}",
    )
    session.add(transaction)
    session.commit()
    return transaction


def _settle_with(session: Session, item: OpenItem, transaction: BankTransaction, **fields):
    return settle_open_item(
        session=session,
        open_item_id=item.id,
        bank_transaction_id=transaction.id,
        changed_by="pytest",
        **fields,
    )


def test_bank_transaction_settles_only_up_to_its_amount(session: Session) -> None:
    """40 EUR Zahlung auf zwei Posten über je 100 EUR: Die Posten sinken zusammen um 40 EUR."""
    company, bank, receivable, _ = _seed_company(session)
    first = _receivable(session, company, receivable, "RE-A")
    second = _receivable(session, company, receivable, "RE-B")
    transaction = _incoming(session, company, bank, "40.00")

    assert _settle_with(session, first, transaction).open_amount == Decimal("60.00")
    for item in (second, first):
        with pytest.raises(OpenItemError, match="über 40.00 EUR ist schon vollständig"):
            _settle_with(session, item, transaction)
        with pytest.raises(OpenItemError, match="über 40.00 EUR ist schon vollständig"):
            _settle_with(session, item, transaction, amount=Decimal("0.01"))

    session.refresh(first)
    session.refresh(second)
    assert (first.open_amount, second.open_amount) == (Decimal("60.00"), Decimal("100.00"))
    assert session.execute(select(OpenItemSettlement)).scalar_one().amount == Decimal("40.00")


def test_bank_transaction_is_shared_across_items_up_to_its_amount(session: Session) -> None:
    """Sammelzahlung: Der Rest des Umsatzes ist die Obergrenze, mit und ohne Betrag."""
    company, bank, receivable, _ = _seed_company(session)
    first = _receivable(session, company, receivable, "RE-A")
    second = _receivable(session, company, receivable, "RE-B")
    third = _receivable(session, company, receivable, "RE-C")
    transaction = _incoming(session, company, bank, "180.00")

    with pytest.raises(OpenItemError, match="noch 180.00 EUR frei"):
        _settle_with(session, first, transaction, amount=Decimal("180.01"))
    assert _settle_with(session, first, transaction, amount=Decimal("70.00")).open_amount == (
        Decimal("30.00")
    )
    assert _settle_with(session, second, transaction).status == "settled"
    with pytest.raises(OpenItemError, match="noch 10.00 EUR frei"):
        _settle_with(session, third, transaction, amount=Decimal("10.01"))
    # Ohne Betrag nimmt der Posten nur den Rest, nicht seinen ganzen offenen Betrag.
    assert _settle_with(session, third, transaction).open_amount == Decimal("90.00")
    with pytest.raises(OpenItemError, match="schon vollständig"):
        _settle_with(session, first, transaction)

    amounts = session.execute(
        select(OpenItemSettlement.amount).order_by(OpenItemSettlement.id)
    ).scalars()
    assert list(amounts) == [Decimal("70.00"), Decimal("100.00"), Decimal("10.00")]


def test_reversed_settlement_frees_the_bank_transaction(session: Session) -> None:
    """Wird die Buchung des Umsatzes storniert, ist sein Betrag wieder frei."""
    company, bank, receivable, _ = _seed_company(session)
    first = _receivable(session, company, receivable, "RE-A")
    second = _receivable(session, company, receivable, "RE-B")
    transaction = _incoming(session, company, bank, "40.00")
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=receivable.id,
        changed_by="pytest",
    )
    _settle_with(session, first, transaction)
    with pytest.raises(OpenItemError, match="schon vollständig"):
        _settle_with(session, second, transaction)

    reverse_journal_entry(
        session=session,
        journal_entry_id=transaction.journal_entry_id,
        reversal_date=date(2026, 7, 10),
        changed_by="pytest",
    )
    session.refresh(first)
    assert first.open_amount == Decimal("100.00")
    book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=receivable.id,
        changed_by="pytest",
    )

    assert _settle_with(session, second, transaction).open_amount == Decimal("60.00")
    with pytest.raises(OpenItemError, match="schon vollständig"):
        _settle_with(session, first, transaction)
    rows = session.execute(
        select(OpenItemSettlement.open_item_id, OpenItemSettlement.reversed_on).order_by(
            OpenItemSettlement.id
        )
    ).all()
    assert rows == [(first.id, date(2026, 7, 10)), (second.id, None)]


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
            "account_type": "asset",
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


def test_open_items_api_create_list_and_settle(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "OPOS API", "company_name": "OPOS API GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "1400",
            "name": "Forderungen",
            "account_type": "asset",
        },
    )

    create_response = client.post(
        "/api/v1/open-items",
        json={
            "company_id": 1,
            "account_id": 1,
            "item_type": "receivable",
            "reference": "RE-API-1",
            "counterparty": "Kunde API",
            "entry_date": "2026-07-01",
            "due_date": "2026-07-15",
            "amount": "1190.00",
        },
    )
    assert create_response.status_code == 201
    item_id = create_response.get_json()["id"]

    list_response = client.get("/api/v1/open-items", query_string={"company_id": 1})
    assert list_response.status_code == 200
    assert list_response.get_json()["open_items"][0]["reference"] == "RE-API-1"

    partial_response = client.post(
        f"/api/v1/open-items/{item_id}/settle",
        json={"amount": "190.00"},
    )
    assert partial_response.status_code == 200
    assert partial_response.get_json()["status"] == "open"
    assert partial_response.get_json()["open_amount"] == "1000.00"

    settle_response = client.post(f"/api/v1/open-items/{item_id}/settle", json={})
    assert settle_response.status_code == 200
    assert settle_response.get_json()["status"] == "settled"
    assert settle_response.get_json()["open_amount"] == "0.00"

    include_settled = client.get(
        "/api/v1/open-items",
        query_string={"company_id": 1, "include_settled": "true"},
    )
    assert include_settled.status_code == 200
    assert include_settled.get_json()["open_items"][0]["status"] == "settled"


def test_used_bank_transaction_is_refused_in_api_and_ui(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/api/v1/tenants", json={"tenant_name": "OPOS Cap", "company_name": "Cap GmbH"})
    for code, name in (("1200", "Bank"), ("1400", "Forderungen")):
        client.post(
            "/api/v1/accounts",
            json={"company_id": 1, "code": code, "name": name, "account_type": "asset"},
        )
    item_ids = [
        client.post(
            "/api/v1/open-items",
            json={
                "company_id": 1,
                "account_id": 2,
                "item_type": "receivable",
                "reference": reference,
                "entry_date": "2026-07-01",
                "amount": "100.00",
            },
        ).get_json()["id"]
        for reference in ("RE-CAP-1", "RE-CAP-2")
    ]
    with app.extensions["db_session_factory"]() as db_session:
        company = db_session.get(Company, 1)
        bank = db_session.execute(select(Account).where(Account.code == "1200")).scalar_one()
        transaction_id = _incoming(db_session, company, bank, "40.00").id

    first = client.post(
        f"/api/v1/open-items/{item_ids[0]}/settle",
        json={"bank_transaction_id": transaction_id},
    )
    assert first.status_code == 200
    assert first.get_json()["open_amount"] == "60.00"
    second = client.post(
        f"/api/v1/open-items/{item_ids[1]}/settle",
        json={"bank_transaction_id": transaction_id},
    )
    assert second.status_code == 422
    assert "schon vollständig" in second.get_json()["error"]

    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    page = client.post(
        f"/offene-posten/{item_ids[1]}/ausgleichen",
        data={"company_id": "1", "bank_transaction_id": str(transaction_id)},
        follow_redirects=True,
    )
    assert "schon vollständig".encode() in page.data
    with app.extensions["db_session_factory"]() as db_session:
        assert db_session.get(OpenItem, item_ids[1]).open_amount == Decimal("100.00")
