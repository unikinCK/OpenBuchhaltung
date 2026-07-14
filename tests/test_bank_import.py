from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.bank_import import (
    BankImportError,
    book_transaction,
    import_bank_csv,
    match_transaction,
    net_from_gross,
    suggest_matches,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    ControllingUnit,
    JournalEntryLine,
    TaxCode,
    Tenant,
    User,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account, Account]:
    tenant = Tenant(name="Bank Tenant")
    company = Company(tenant=tenant, name="Bank GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    rent = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="4200",
        name="Miete",
        account_type="expense",
    )
    vat_in = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1576",
        name="Vorsteuer 19 %",
        account_type="asset",
    )
    revenue = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="income",
    )
    session.add_all([bank, rent, vat_in, revenue])
    session.commit()
    return company, bank, rent


GERMAN_CSV = """Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag
05.07.2026;Zahlungseingang RE-1001;Kunde AG;1.190,00
06.07.2026;Miete Juli;Vermieter GmbH;-595,00
07.07.2026;Gebühren;Hausbank;-9,90
"""


def test_import_bank_csv_german_format_and_dedup(session: Session) -> None:
    company, bank, _ = _seed_company(session)

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert report.imported_rows == 3
    assert report.error_rows == 0

    transactions = session.execute(
        select(BankTransaction).order_by(BankTransaction.booking_date)
    ).scalars().all()
    assert transactions[0].amount == Decimal("1190.00")
    assert transactions[0].booking_date == date(2026, 7, 5)
    assert transactions[1].amount == Decimal("-595.00")
    assert transactions[0].status == "open"

    # Re-Import ist idempotent
    second = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert second.imported_rows == 0
    assert second.duplicate_rows == 3

    audit = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "bank_import")
    ).scalars().all()
    assert len(audit) == 2


def test_import_reports_row_errors(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    broken_csv = "Buchungstag;Verwendungszweck;Betrag\nkein-datum;Test;10,00\n05.07.2026;;5,00\n"

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(broken_csv),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.error_rows == 2


def test_suggest_and_match_transaction(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    revenue_id = session.execute(
        select(Account.id).where(Account.company_id == company.id, Account.code == "8400")
    ).scalar_one()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 4),
            description="Ausgangsrechnung RE-1001",
            status="posted",
            lines=[
                JournalLineInput(bank.id, Decimal("1190.00"), Decimal("0.00")),
                JournalLineInput(revenue_id, Decimal("0.00"), Decimal("1190.00")),
            ],
        ),
    )

    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    incoming = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("1190.00"))
    ).scalar_one()

    suggestions = suggest_matches(session=session, transaction=incoming)
    assert [suggestion.id for suggestion in suggestions] == [entry.id]

    matched = match_transaction(
        session=session,
        transaction_id=incoming.id,
        journal_entry_id=entry.id,
        changed_by="tester",
    )
    assert matched.status == "matched"
    assert matched.journal_entry_id == entry.id

    # Bereits verknüpfte Buchungen werden nicht mehr vorgeschlagen
    assert suggest_matches(session=session, transaction=incoming) == []

    with pytest.raises(BankImportError, match="bereits zugeordnet"):
        match_transaction(
            session=session,
            transaction_id=incoming.id,
            journal_entry_id=entry.id,
            changed_by="tester",
        )


def test_book_transaction_with_tax_code_splits_gross(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    cost_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="cost_center",
        code="K100",
        name="Verwaltung",
    )
    profit_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="profit_center",
        code="P100",
        name="Zentrale",
    )
    session.add_all([cost_center, profit_center])
    session.commit()
    ensure_default_tax_codes(session=session, company=company)
    vst19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "VSt19")
    ).scalar_one()

    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()

    booked = book_transaction(
        session=session,
        transaction_id=outgoing.id,
        contra_account_id=rent.id,
        tax_code_id=vst19.id,
        cost_center_id=cost_center.id,
        profit_center_id=profit_center.id,
        changed_by="tester",
    )
    assert booked.status == "booked"

    lines = session.execute(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == booked.journal_entry_id)
        .order_by(JournalEntryLine.line_number)
    ).scalars().all()
    # Bank 595 Haben, Miete 500 Soll, Vorsteuer 95 Soll
    assert len(lines) == 3
    assert lines[0].credit_amount == Decimal("595.00")
    assert lines[1].debit_amount == Decimal("500.00")
    assert lines[2].debit_amount == Decimal("95.00")
    assert lines[0].cost_center_id is None
    assert lines[0].profit_center_id is None
    assert all(line.cost_center_id == cost_center.id for line in lines[1:])
    assert all(line.profit_center_id == profit_center.id for line in lines[1:])


def test_net_from_gross_edge_cases() -> None:
    assert net_from_gross(Decimal("119.00"), Decimal("19.00")) == (
        Decimal("100.00"),
        Decimal("19.00"),
    )
    assert net_from_gross(Decimal("595.00"), Decimal("19.00")) == (
        Decimal("500.00"),
        Decimal("95.00"),
    )
    net, tax = net_from_gross(Decimal("0.02"), Decimal("19.00"))
    assert net + tax == Decimal("0.02")
    assert net_from_gross(Decimal("50.00"), Decimal("0.00")) == (
        Decimal("50.00"),
        Decimal("0.00"),
    )
    # 0,03 € hat keine exakte Netto+19%-Zerlegung — muss sauber fehlschlagen
    with pytest.raises(BankImportError, match="zerlegen"):
        net_from_gross(Decimal("0.03"), Decimal("19.00"))


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_bank.db'}",
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


def test_bank_page_upload_and_book_flow(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )

    upload_response = client.post(
        "/bank/import",
        data={
            "company_id": "1",
            "bank_account_id": "1",
            "bank_csv": (BytesIO(GERMAN_CSV.encode("utf-8")), "umsaetze.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert upload_response.status_code == 200
    assert b"3 neu" in upload_response.data
    assert b"Zahlungseingang RE-1001" in upload_response.data

    book_response = client.post(
        "/bank/2/buchen",
        data={"company_id": "1", "contra_account_id": "2"},
        follow_redirects=True,
    )
    assert book_response.status_code == 200
    assert b"wurde verbucht" in book_response.data
    assert b"verbucht</span>" in book_response.data


def test_bank_api_import_list_match_and_book_flow(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "8400", "name": "Erlöse", "account_type": "income"},
    )

    with app.extensions["db_session_factory"]() as db_session:
        bank_id = db_session.execute(select(Account.id).where(Account.code == "1200")).scalar_one()
        rent_id = db_session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()
        revenue_id = db_session.execute(
            select(Account.id).where(Account.code == "8400")
        ).scalar_one()
        matching_entry = create_journal_entry(
            session=db_session,
            payload=JournalEntryInput(
                company_id=1,
                entry_date=date(2026, 7, 4),
                description="Ausgangsrechnung RE-1001",
                status="posted",
                lines=[
                    JournalLineInput(bank_id, Decimal("1190.00"), Decimal("0.00")),
                    JournalLineInput(revenue_id, Decimal("0.00"), Decimal("1190.00")),
                ],
            ),
        )
        matching_entry_id = matching_entry.id

    import_response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": 1,
            "bank_account_id": bank_id,
            "file_name": "umsaetze.csv",
            "mime_type": "text/csv",
            "content_base64": base64.b64encode(GERMAN_CSV.encode("utf-8")).decode("ascii"),
        },
    )
    assert import_response.status_code == 201
    assert import_response.get_json()["report"]["imported_rows"] == 3

    list_response = client.get(
        "/api/v1/bank-transactions",
        query_string={"company_id": 1, "include_suggestions": "true"},
    )
    assert list_response.status_code == 200
    transactions = list_response.get_json()["transactions"]
    incoming = next(tx for tx in transactions if tx["amount"] == "1190.00")
    outgoing = next(tx for tx in transactions if tx["amount"] == "-595.00")
    assert incoming["suggestions"][0]["id"] == matching_entry_id

    match_response = client.post(
        f"/api/v1/bank-transactions/{incoming['id']}/match",
        json={"journal_entry_id": matching_entry_id},
    )
    assert match_response.status_code == 200
    assert match_response.get_json()["status"] == "matched"

    book_response = client.post(
        f"/api/v1/bank-transactions/{outgoing['id']}/book",
        json={"contra_account_id": rent_id, "description": "Miete Juli"},
    )
    assert book_response.status_code == 201
    booked = book_response.get_json()
    assert booked["status"] == "booked"
    assert booked["journal_entry_id"] is not None
