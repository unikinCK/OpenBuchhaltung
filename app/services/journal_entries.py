from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import (
    Account,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    Period,
    PeriodLock,
)
from domain.services.journal_entry_validation import (
    JournalEntryDraft,
    JournalEntryValidator,
    ValidationLine,
)


class JournalEntryCreationError(ValueError):
    """Raised when a journal entry request cannot be persisted."""


@dataclass(slots=True)
class JournalLineInput:
    account_id: int
    debit_amount: Decimal
    credit_amount: Decimal
    description: str | None = None


@dataclass(slots=True)
class JournalEntryInput:
    company_id: int
    entry_date: date
    description: str
    status: str
    lines: list[JournalLineInput]
    changed_by: str = "system"


def create_journal_entry(*, session: Session, payload: JournalEntryInput) -> JournalEntry:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise JournalEntryCreationError("Gesellschaft nicht gefunden.")

    JournalEntryValidator.validate(
        JournalEntryDraft(
            status=payload.status,
            lines=[
                ValidationLine(
                    account_id=line.account_id,
                    debit_amount=line.debit_amount,
                    credit_amount=line.credit_amount,
                )
                for line in payload.lines
            ],
        )
    )

    accounts = {
        account.id: account
        for account in session.execute(
            select(Account).where(
                Account.company_id == company.id,
                Account.id.in_([line.account_id for line in payload.lines]),
            )
        )
        .scalars()
        .all()
    }

    for line in payload.lines:
        account = accounts.get(line.account_id)
        if account is None:
            raise JournalEntryCreationError("Konto für Buchungszeile nicht gefunden.")
        if not account.is_active:
            raise JournalEntryCreationError("Inaktive Konten dürfen nicht bebucht werden.")

    fiscal_year = _get_or_create_fiscal_year(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        dt=payload.entry_date,
    )
    period = _get_or_create_period(
        session=session,
        tenant_id=company.tenant_id,
        fiscal_year_id=fiscal_year.id,
        dt=payload.entry_date,
    )
    _ensure_period_is_open(session=session, period_id=period.id)

    posting_number = _next_posting_number(
        session=session,
        company_id=company.id,
        year=payload.entry_date.year,
    )

    entry = JournalEntry(
        tenant_id=company.tenant_id,
        company_id=company.id,
        fiscal_year_id=fiscal_year.id,
        period_id=period.id,
        posting_number=posting_number,
        entry_date=payload.entry_date,
        description=payload.description,
        source="manual",
    )
    session.add(entry)
    session.flush()

    for idx, line in enumerate(payload.lines, start=1):
        line_payload = {
            "tenant_id": company.tenant_id,
            "journal_entry_id": entry.id,
            "line_number": idx,
            "account_id": line.account_id,
            "description": line.description,
            "currency_code": company.currency_code,
        }
        if line.debit_amount > Decimal("0.00"):
            line_payload["debit_amount"] = line.debit_amount
        if line.credit_amount > Decimal("0.00"):
            line_payload["credit_amount"] = line.credit_amount

        session.add(JournalEntryLine(**line_payload))

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="journal_entry",
        entity_id=str(entry.id),
        action="created",
        changed_by=payload.changed_by,
        payload={
            "posting_number": entry.posting_number,
            "entry_date": entry.entry_date.isoformat(),
            "description": entry.description,
            "line_count": len(payload.lines),
        },
    )

    session.commit()
    session.refresh(entry)
    return entry


def parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        raise JournalEntryCreationError("Betrag ist keine gültige Dezimalzahl.") from None


def _get_or_create_fiscal_year(
    *,
    session: Session,
    tenant_id: int,
    company_id: int,
    dt: date,
) -> FiscalYear:
    label = str(dt.year)
    fiscal_year = session.execute(
        select(FiscalYear).where(FiscalYear.company_id == company_id, FiscalYear.label == label)
    ).scalar_one_or_none()
    if fiscal_year:
        return fiscal_year

    fiscal_year = FiscalYear(
        tenant_id=tenant_id,
        company_id=company_id,
        label=label,
        start_date=date(dt.year, 1, 1),
        end_date=date(dt.year, 12, 31),
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()
    return fiscal_year


def _get_or_create_period(
    *,
    session: Session,
    tenant_id: int,
    fiscal_year_id: int,
    dt: date,
) -> Period:
    period = session.execute(
        select(Period).where(
            Period.fiscal_year_id == fiscal_year_id,
            Period.period_number == dt.month,
        )
    ).scalar_one_or_none()
    if period:
        return period

    end_day = 31
    if dt.month in {4, 6, 9, 11}:
        end_day = 30
    if dt.month == 2:
        end_day = 29 if (dt.year % 4 == 0 and (dt.year % 100 != 0 or dt.year % 400 == 0)) else 28

    period = Period(
        tenant_id=tenant_id,
        fiscal_year_id=fiscal_year_id,
        period_number=dt.month,
        start_date=date(dt.year, dt.month, 1),
        end_date=date(dt.year, dt.month, end_day),
        status="open",
    )
    session.add(period)
    session.flush()
    return period


def _next_posting_number(*, session: Session, company_id: int, year: int) -> str:
    count = (
        session.scalar(
            select(func.count(JournalEntry.id)).where(JournalEntry.company_id == company_id)
        )
        or 0
    )
    return f"{year}-{count + 1:04d}"


def _ensure_period_is_open(*, session: Session, period_id: int) -> None:
    locked = session.execute(select(PeriodLock.id).where(PeriodLock.period_id == period_id)).first()
    if locked:
        raise JournalEntryCreationError("Die Periode ist gesperrt. Buchung nicht möglich.")
