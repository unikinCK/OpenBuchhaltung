from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, AuditLog, Base, Company, FiscalYear, Period, PeriodLock, Tenant


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


def _seed_company_and_accounts(session: Session) -> Company:
    tenant = Tenant(name="Audit Tenant")
    company = Company(tenant=tenant, name="Audit GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    session.add_all(
        [
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="1200",
                name="Bank",
                account_type="asset",
            ),
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="8400",
                name="Erlöse",
                account_type="revenue",
            ),
        ]
    )
    session.commit()
    return company


def test_create_journal_entry_writes_audit_log(session: Session) -> None:
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 4),
            description="Audit-Test",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=account_ids[0],
                    debit_amount=Decimal("100.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=account_ids[1],
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("100.00"),
                ),
            ],
        ),
    )

    audit_log = session.scalar(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry",
            AuditLog.entity_id == str(entry.id),
            AuditLog.action == "created",
        )
    )
    assert audit_log is not None
    assert audit_log.changed_by == "pytest"
    assert audit_log.payload is not None
    assert audit_log.payload["posting_number"] == entry.posting_number


def test_create_journal_entry_resolves_account_codes(session: Session) -> None:
    company = _seed_company_and_accounts(session)

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 4),
            description="Buchung per Kontonummer",
            status="posted",
            lines=[
                JournalLineInput(account_code="1200", debit_amount=Decimal("100.00")),
                JournalLineInput(account_code="8400", credit_amount=Decimal("100.00")),
            ],
        ),
    )

    id_by_code = {
        account.code: account.id
        for account in session.scalars(
            select(Account).where(Account.company_id == company.id)
        ).all()
    }
    booked_account_ids = {line.account_id for line in entry.lines}
    assert booked_account_ids == {id_by_code["1200"], id_by_code["8400"]}


def test_create_journal_entry_rejects_unknown_account_code(session: Session) -> None:
    company = _seed_company_and_accounts(session)

    with pytest.raises(JournalEntryCreationError, match="9999"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 4, 4),
                description="Unbekannte Kontonummer",
                status="posted",
                lines=[
                    JournalLineInput(account_code="9999", debit_amount=Decimal("100.00")),
                    JournalLineInput(account_code="8400", credit_amount=Decimal("100.00")),
                ],
            ),
        )


def test_create_journal_entry_rejects_locked_period(session: Session) -> None:
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()

    fiscal_year = FiscalYear(
        tenant_id=company.tenant_id,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()

    period = Period(
        tenant_id=company.tenant_id,
        fiscal_year_id=fiscal_year.id,
        period_number=4,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        status="closed",
    )
    session.add(period)
    session.flush()

    session.add(
        PeriodLock(
            tenant_id=company.tenant_id,
            period_id=period.id,
            reason="Monatsabschluss",
            locked_by="pytest",
            locked_at=datetime.now(timezone.utc),
        )
    )
    session.commit()

    with pytest.raises(JournalEntryCreationError, match="Periode ist gesperrt"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 4, 10),
                description="Gesperrte-Periode-Test",
                status="posted",
                changed_by="pytest",
                lines=[
                    JournalLineInput(
                        account_id=account_ids[0],
                        debit_amount=Decimal("50.00"),
                        credit_amount=Decimal("0.00"),
                    ),
                    JournalLineInput(
                        account_id=account_ids[1],
                        debit_amount=Decimal("0.00"),
                        credit_amount=Decimal("50.00"),
                    ),
                ],
            ),
        )


def test_create_journal_entry_fallback_period_number_stays_in_range(session: Session) -> None:
    """Fehlt für einen Monat die Periode, darf der Fallback keine Nummer > 13 vergeben.

    Reproduziert den Produktionsfehler ``CHECK constraint failed:
    ck_period_number_range``: Ein Altbestand mit einer bereits als Nummer 13
    angelegten regulären Periode führte beim Buchen eines späteren Monats zu
    ``max_number + 1 == 14`` und damit zum Constraint-Verstoß.
    """
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()

    fiscal_year = FiscalYear(
        tenant_id=company.tenant_id,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()

    # Unvollständiger Altbestand: nur der Juli existiert, versehentlich als
    # reguläre Periode mit der höchsten zulässigen Nummer 13.
    session.add(
        Period(
            tenant_id=company.tenant_id,
            fiscal_year_id=fiscal_year.id,
            period_number=13,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            status="open",
            is_closing=False,
        )
    )
    session.commit()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 8, 15),
            description="August-Buchung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(
                    account_id=account_ids[0],
                    debit_amount=Decimal("50.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=account_ids[1],
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("50.00"),
                ),
            ],
        ),
    )

    period = session.get(Period, entry.period_id)
    assert period is not None
    # August ist der 8. Monat des Kalender-WJ – deterministisch, nicht max+1.
    assert period.period_number == 8
    assert 1 <= period.period_number <= 13


def _seed_fiscal_year(session: Session, company: Company) -> FiscalYear:
    fiscal_year = FiscalYear(
        tenant_id=company.tenant_id,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()
    return fiscal_year


def _balanced_lines(account_ids: list[int]) -> list[JournalLineInput]:
    return [
        JournalLineInput(
            account_id=account_ids[0],
            debit_amount=Decimal("50.00"),
            credit_amount=Decimal("0.00"),
        ),
        JournalLineInput(
            account_id=account_ids[1],
            debit_amount=Decimal("0.00"),
            credit_amount=Decimal("50.00"),
        ),
    ]


def test_fallback_uses_free_number_when_deterministic_number_is_taken(
    session: Session,
) -> None:
    """Altbestand mit Buchungsreihenfolge-Nummerierung (max+1) darf nicht kollidieren.

    Reproduziert den Produktionsfehler ``UNIQUE constraint failed:
    period.fiscal_year_id, period.period_number``: Eine Legacy-Periode trägt die
    Nummer 8, deckt aber den Oktober ab. Eine August-Buchung berechnet
    deterministisch ebenfalls Nummer 8 und lief damit in den UNIQUE-Constraint.
    """
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()
    fiscal_year = _seed_fiscal_year(session, company)

    # Legacy-Periode: Nummer 8 wurde in Buchungsreihenfolge vergeben und
    # umfasst den Oktober – nicht den August.
    session.add(
        Period(
            tenant_id=company.tenant_id,
            fiscal_year_id=fiscal_year.id,
            period_number=8,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 31),
            status="open",
            is_closing=False,
        )
    )
    session.commit()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 8, 15),
            description="August-Buchung trotz Legacy-Nummerierung",
            status="posted",
            changed_by="pytest",
            lines=_balanced_lines(account_ids),
        ),
    )

    period = session.get(Period, entry.period_id)
    assert period is not None
    assert period.start_date == date(2026, 8, 1)
    assert period.end_date == date(2026, 8, 31)
    # Nummer 8 ist belegt – es muss eine freie Nummer im Bereich 1..12 sein.
    assert 1 <= period.period_number <= 12
    assert period.period_number != 8


def test_fallback_raises_clear_error_when_no_period_number_is_free(
    session: Session,
) -> None:
    """Sind alle Nummern 1..12 belegt, aber keine Periode passt zum Datum,
    muss eine verständliche Fehlermeldung statt eines DB-Fehlers erscheinen."""
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()
    fiscal_year = _seed_fiscal_year(session, company)

    # Inkonsistenter Altbestand: 12 Perioden, alle im Januar – keine deckt den August ab.
    for number in range(1, 13):
        session.add(
            Period(
                tenant_id=company.tenant_id,
                fiscal_year_id=fiscal_year.id,
                period_number=number,
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
                status="open",
                is_closing=False,
            )
        )
    session.commit()

    with pytest.raises(JournalEntryCreationError, match="Periodennummern 1–12"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 8, 15),
                description="August-Buchung ohne freie Periodennummer",
                status="posted",
                changed_by="pytest",
                lines=_balanced_lines(account_ids),
            ),
        )


def test_closing_period_creation_fails_cleanly_when_number_13_is_taken(
    session: Session,
) -> None:
    """Belegt eine reguläre Legacy-Periode die Nummer 13, darf die Buchung in die
    Abschlussperiode nicht mit einem UNIQUE-Fehler crashen (betrifft u. a. die
    AfA-Buchungen der Anlagenbuchhaltung und den Jahresabschluss)."""
    company = _seed_company_and_accounts(session)
    account_ids = session.scalars(
        select(Account.id).where(Account.company_id == company.id).order_by(Account.code)
    ).all()
    fiscal_year = _seed_fiscal_year(session, company)

    session.add(
        Period(
            tenant_id=company.tenant_id,
            fiscal_year_id=fiscal_year.id,
            period_number=13,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 31),
            status="open",
            is_closing=False,
        )
    )
    session.commit()

    with pytest.raises(JournalEntryCreationError, match="Abschlussperiode"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 12, 31),
                description="Abschlussbuchung",
                status="posted",
                changed_by="pytest",
                post_to_closing_period=True,
                lines=_balanced_lines(account_ids),
            ),
        )
