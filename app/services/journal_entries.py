from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
    TaxCode,
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
    tax_code_id: int | None = None


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

    lines = _expand_tax_lines(session=session, company=company, lines=payload.lines)

    JournalEntryValidator.validate(
        JournalEntryDraft(
            status=payload.status,
            lines=[
                ValidationLine(
                    account_id=line.account_id,
                    debit_amount=line.debit_amount,
                    credit_amount=line.credit_amount,
                )
                for line in lines
            ],
        )
    )

    accounts = {
        account.id: account
        for account in session.execute(
            select(Account).where(
                Account.company_id == company.id,
                Account.id.in_([line.account_id for line in lines]),
            )
        )
        .scalars()
        .all()
    }

    for line in lines:
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

    for idx, line in enumerate(lines, start=1):
        line_payload = {
            "tenant_id": company.tenant_id,
            "journal_entry_id": entry.id,
            "line_number": idx,
            "account_id": line.account_id,
            "description": line.description,
            "currency_code": company.currency_code,
            "tax_code_id": line.tax_code_id,
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
            "line_count": len(lines),
        },
    )

    session.commit()
    session.refresh(entry)
    return entry


def _expand_tax_lines(
    *,
    session: Session,
    company: Company,
    lines: list[JournalLineInput],
) -> list[JournalLineInput]:
    """Erzeugt für Zeilen mit Steuercode die automatische USt-/VSt-Teilbuchung.

    Beträge der Ursprungszeile gelten als Netto; die Steuerzeile wird auf derselben
    Seite (Soll/Haben) auf dem Steuerkonto des Steuercodes ergänzt.
    """
    tax_code_ids = {line.tax_code_id for line in lines if line.tax_code_id is not None}
    if not tax_code_ids:
        return list(lines)

    tax_codes = {
        tax_code.id: tax_code
        for tax_code in session.execute(
            select(TaxCode).where(
                TaxCode.company_id == company.id,
                TaxCode.id.in_(tax_code_ids),
            )
        )
        .scalars()
        .all()
    }

    expanded: list[JournalLineInput] = []
    for line in lines:
        expanded.append(line)
        if line.tax_code_id is None:
            continue

        tax_code = tax_codes.get(line.tax_code_id)
        if tax_code is None:
            raise JournalEntryCreationError("Steuercode für Buchungszeile nicht gefunden.")
        if not tax_code.is_active:
            raise JournalEntryCreationError("Inaktive Steuercodes dürfen nicht verwendet werden.")
        if tax_code.rate == Decimal("0.00"):
            continue
        if tax_code.vat_account_id is None:
            raise JournalEntryCreationError(
                f"Steuercode {tax_code.code} hat kein Steuerkonto hinterlegt."
            )

        net_amount = line.debit_amount if line.debit_amount > 0 else line.credit_amount
        tax_amount = (net_amount * tax_code.rate / Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if tax_amount <= Decimal("0.00"):
            continue

        is_debit = line.debit_amount > 0
        expanded.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                debit_amount=tax_amount if is_debit else Decimal("0.00"),
                credit_amount=tax_amount if not is_debit else Decimal("0.00"),
                description=f"{tax_code.code} {tax_code.rate}% auf {net_amount}",
                tax_code_id=tax_code.id,
            )
        )

    return expanded


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
