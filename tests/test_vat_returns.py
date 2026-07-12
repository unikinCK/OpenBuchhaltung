"""Tests für die UStVA-Kennziffern-Berechnung und Snapshots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    list_vat_returns,
    period_bounds,
    save_vat_return,
)
from domain.models import Account, AuditLog, Base, Company, TaxCode, Tenant


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> Company:
    tenant = Tenant(name="UStVA Tenant")
    company = Company(tenant=tenant, name="UStVA GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    accounts = {
        "1200": ("Bank", "asset"),
        "1400": ("Forderungen", "asset"),
        "1576": ("Abziehbare Vorsteuer 19 %", "asset"),
        "1776": ("Umsatzsteuer 19 %", "liability"),
        "1771": ("Umsatzsteuer 7 %", "liability"),
        "1600": ("Verbindlichkeiten", "liability"),
        "4200": ("Miete", "expense"),
        "8400": ("Erlöse 19 %", "revenue"),
        "8300": ("Erlöse 7 %", "revenue"),
        "8100": ("Steuerfreie Erlöse", "revenue"),
    }
    for code, (name, account_type) in accounts.items():
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
            )
        )
    session.flush()

    def account_id(code: str) -> int:
        return session.scalar(
            select(Account.id).where(Account.company_id == company.id, Account.code == code)
        )

    session.add_all(
        [
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="USt19",
                rate=Decimal("19.00"),
                vat_account_id=account_id("1776"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="USt7",
                rate=Decimal("7.00"),
                vat_account_id=account_id("1771"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="VSt19",
                rate=Decimal("19.00"),
                vat_account_id=account_id("1576"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="frei",
                rate=Decimal("0.00"),
            ),
        ]
    )
    session.commit()
    return company


def _account_id(session: Session, company: Company, code: str) -> int:
    return session.scalar(
        select(Account.id).where(Account.company_id == company.id, Account.code == code)
    )


def _tax_code_id(session: Session, company: Company, code: str) -> int:
    return session.scalar(
        select(TaxCode.id).where(TaxCode.company_id == company.id, TaxCode.code == code)
    )


def _book(session: Session, company: Company, *, entry_date, description, lines) -> None:
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description=description,
            status="posted",
            changed_by="pytest",
            lines=lines,
        ),
    )


def _seed_bookings(session: Session, company: Company) -> None:
    """Mai 2026: 1.000,50 € netto zu 19 %, 200 € netto zu 7 %, 100 € steuerfrei,
    Eingangsrechnung 500 € netto mit 19 % Vorsteuer."""
    _book(
        session,
        company,
        entry_date=date(2026, 5, 5),
        description="Ausgangsrechnung 19 %",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("1190.60"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8400"),
                credit_amount=Decimal("1000.50"),
                tax_code_id=_tax_code_id(session, company, "USt19"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 12),
        description="Ausgangsrechnung 7 %",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("214.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8300"),
                credit_amount=Decimal("200.00"),
                tax_code_id=_tax_code_id(session, company, "USt7"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 20),
        description="Steuerfreier Umsatz",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1200"),
                debit_amount=Decimal("100.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8100"),
                credit_amount=Decimal("100.00"),
                tax_code_id=_tax_code_id(session, company, "frei"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 25),
        description="Eingangsrechnung Miete",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "4200"),
                debit_amount=Decimal("500.00"),
                tax_code_id=_tax_code_id(session, company, "VSt19"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "1600"),
                credit_amount=Decimal("595.00"),
            ),
        ],
    )
    # Juni-Buchung: darf im Mai nicht auftauchen.
    _book(
        session,
        company,
        entry_date=date(2026, 6, 2),
        description="Ausgangsrechnung Juni",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("119.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8400"),
                credit_amount=Decimal("100.00"),
                tax_code_id=_tax_code_id(session, company, "USt19"),
            ),
        ],
    )


def _amounts_by_kz(rows) -> dict[str, Decimal]:
    return {row.kennziffer: row.amount for row in rows}


def test_period_bounds() -> None:
    assert period_bounds("2026-05") == (date(2026, 5, 1), date(2026, 5, 31), "2026-05")
    assert period_bounds("2026-q2") == (date(2026, 4, 1), date(2026, 6, 30), "2026-Q2")
    assert period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31), "2026-12")
    with pytest.raises(VatReturnError):
        period_bounds("2026-13")
    with pytest.raises(VatReturnError):
        period_bounds("2026-Q5")
    with pytest.raises(VatReturnError):
        period_bounds("Mai 2026")


def test_compute_vat_return_monthly(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    rows = compute_vat_return(
        session=session,
        company_id=company.id,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    amounts = _amounts_by_kz(rows)

    # Bemessungsgrundlagen in vollen Euro abgerundet (1000,50 -> 1000).
    assert amounts["81"] == Decimal("1000")
    assert amounts["86"] == Decimal("200")
    assert amounts["48"] == Decimal("100")
    # Gebuchte Steuer: 19 % von 1000,50 = 190,10 (halb-auf) + 7 % von 200 = 14,00.
    assert amounts["USt"] == Decimal("204.10")
    assert amounts["66"] == Decimal("95.00")
    assert amounts["83"] == Decimal("109.10")


def test_compute_vat_return_quarter_includes_june(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    date_from, date_to, label = period_bounds("2026-Q2")
    amounts = _amounts_by_kz(
        compute_vat_return(
            session=session, company_id=company.id, date_from=date_from, date_to=date_to
        )
    )
    assert label == "2026-Q2"
    assert amounts["81"] == Decimal("1100")  # 1000,50 + 100 -> abgerundet
    assert amounts["83"] == Decimal("128.10")


def test_storno_neutralizes_vat_return(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    before = _amounts_by_kz(
        compute_vat_return(
            session=session,
            company_id=company.id,
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
        )
    )
    assert before["81"] == Decimal("1000")

    # Die 19 %-Ausgangsrechnung im Mai stornieren (Storno im selben Zeitraum).
    from domain.models import JournalEntry

    entry_id = session.scalar(
        select(JournalEntry.id).where(
            JournalEntry.company_id == company.id,
            JournalEntry.description == "Ausgangsrechnung 19 %",
        )
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry_id,
        reversal_date=date(2026, 5, 28),
        changed_by="pytest",
    )

    after = _amounts_by_kz(
        compute_vat_return(
            session=session,
            company_id=company.id,
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
        )
    )
    assert after["81"] == Decimal("0")
    assert after["USt"] == Decimal("14.00")
    assert after["83"] == Decimal("-81.00")


def test_save_vat_return_snapshot_and_duplicate(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )
    assert vat_return.period_label == "2026-05"
    assert vat_return.status == "erstellt"
    amounts = {row["kennziffer"]: row["amount"] for row in vat_return.kennzahlen}
    assert amounts["83"] == "109.10"

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "vat_return",
            AuditLog.entity_id == str(vat_return.id),
            AuditLog.action == "created",
        )
    ).scalar_one()
    assert audit.payload["period_label"] == "2026-05"

    with pytest.raises(VatReturnError, match="bereits eine UStVA"):
        save_vat_return(
            session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
        )

    items = list_vat_returns(session=session, company_id=company.id)
    assert [item.period_label for item in items] == ["2026-05"]


def test_save_vat_return_normalizes_label(session: Session) -> None:
    company = _seed(session)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-q2", changed_by="pytest"
    )
    assert vat_return.period_label == "2026-Q2"
