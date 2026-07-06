from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import (
    Account,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    Period,
    PeriodLock,
)

# Gewinnvortrag vor Verwendung: SKR03 = 0860, SKR04 = 2970
RETAINED_EARNINGS_CODES = ("0860", "2970")
PROFIT_AND_LOSS_ACCOUNT_TYPES = ("income", "revenue", "expense")


class PeriodActionError(ValueError):
    """Raised when a period/fiscal-year action is not allowed."""


@dataclass(slots=True)
class FiscalYearCloseResult:
    fiscal_year: FiscalYear
    carryforward_entry: JournalEntry | None


def _company_for_period(session: Session, period: Period) -> Company:
    fiscal_year = session.get(FiscalYear, period.fiscal_year_id)
    company = session.get(Company, fiscal_year.company_id)
    return company


def lock_period(
    *,
    session: Session,
    period_id: int,
    locked_by: str,
    reason: str | None = None,
) -> Period:
    period = session.get(Period, period_id)
    if period is None:
        raise PeriodActionError("Periode nicht gefunden.")

    existing_lock = session.execute(
        select(PeriodLock.id).where(PeriodLock.period_id == period.id)
    ).first()
    if existing_lock:
        raise PeriodActionError("Die Periode ist bereits gesperrt.")

    session.add(
        PeriodLock(
            tenant_id=period.tenant_id,
            period_id=period.id,
            reason=reason,
            locked_by=locked_by,
        )
    )
    period.status = "locked"

    company = _company_for_period(session, period)
    log_audit_event(
        session=session,
        tenant_id=period.tenant_id,
        company_id=company.id,
        entity_type="period",
        entity_id=str(period.id),
        action="locked",
        changed_by=locked_by,
        payload={"period_number": period.period_number, "reason": reason},
    )
    session.commit()
    session.refresh(period)
    return period


def unlock_period(*, session: Session, period_id: int, changed_by: str) -> Period:
    period = session.get(Period, period_id)
    if period is None:
        raise PeriodActionError("Periode nicht gefunden.")

    fiscal_year = session.get(FiscalYear, period.fiscal_year_id)
    if fiscal_year.is_closed:
        raise PeriodActionError(
            "Das Geschäftsjahr ist abgeschlossen; Perioden können nicht entsperrt werden."
        )

    locks = (
        session.execute(select(PeriodLock).where(PeriodLock.period_id == period.id))
        .scalars()
        .all()
    )
    if not locks:
        raise PeriodActionError("Die Periode ist nicht gesperrt.")

    for lock in locks:
        session.delete(lock)
    period.status = "open"

    company = _company_for_period(session, period)
    log_audit_event(
        session=session,
        tenant_id=period.tenant_id,
        company_id=company.id,
        entity_type="period",
        entity_id=str(period.id),
        action="unlocked",
        changed_by=changed_by,
        payload={"period_number": period.period_number},
    )
    session.commit()
    session.refresh(period)
    return period


def _find_retained_earnings_account(session: Session, company_id: int) -> Account | None:
    account = session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_(RETAINED_EARNINGS_CODES),
            Account.is_active.is_(True),
        )
    ).scalars().first()
    if account is not None:
        return account
    return session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.name.ilike("%gewinnvortrag%"),
            Account.is_active.is_(True),
        )
    ).scalars().first()


def _create_carryforward_entry(
    *,
    session: Session,
    fiscal_year: FiscalYear,
    retained_account: Account,
    changed_by: str,
) -> JournalEntry | None:
    """Schließt die Erfolgskonten des Geschäftsjahres gegen den Gewinnvortrag ab."""
    rows = session.execute(
        select(
            Account.id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntryLine, JournalEntryLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(
            JournalEntry.fiscal_year_id == fiscal_year.id,
            Account.account_type.in_(PROFIT_AND_LOSS_ACCOUNT_TYPES),
        )
        .group_by(Account.id)
    ).all()

    zero = Decimal("0.00")
    lines: list[JournalLineInput] = []
    net_income = zero
    for account_id, debit_total, credit_total in rows:
        saldo = Decimal(debit_total) - Decimal(credit_total)
        if saldo == zero:
            continue
        # Konto glattstellen: Sollsaldo (Aufwand) wird im Haben abgeschlossen und umgekehrt
        lines.append(
            JournalLineInput(
                account_id=account_id,
                debit_amount=-saldo if saldo < zero else zero,
                credit_amount=saldo if saldo > zero else zero,
            )
        )
        net_income -= saldo

    if not lines:
        return None

    lines.append(
        JournalLineInput(
            account_id=retained_account.id,
            debit_amount=-net_income if net_income < zero else zero,
            credit_amount=net_income if net_income > zero else zero,
        )
    )
    if net_income == zero:
        # Erfolgskonten gleichen sich exakt aus — keine Vortragszeile nötig
        lines = lines[:-1]

    try:
        return create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=fiscal_year.company_id,
                entry_date=fiscal_year.end_date,
                description=f"Ergebnisvortrag {fiscal_year.label}",
                status="posted",
                changed_by=changed_by,
                lines=lines,
            ),
        )
    except JournalEntryCreationError as exc:
        raise PeriodActionError(
            f"Ergebnisvortrag konnte nicht gebucht werden: {exc} "
            f"(ggf. Periode {fiscal_year.end_date.month} vorher entsperren)."
        ) from exc


def close_fiscal_year(
    *, session: Session, fiscal_year_id: int, changed_by: str
) -> FiscalYearCloseResult:
    """Schließt ein Geschäftsjahr ab.

    Reihenfolge: erst Ergebnisvortrag buchen (GuV-Konten gegen Gewinnvortrag
    glattstellen), dann alle Perioden sperren und das Jahr als abgeschlossen
    markieren.
    """
    fiscal_year = session.get(FiscalYear, fiscal_year_id)
    if fiscal_year is None:
        raise PeriodActionError("Geschäftsjahr nicht gefunden.")
    if fiscal_year.is_closed:
        raise PeriodActionError("Das Geschäftsjahr ist bereits abgeschlossen.")

    retained_account = _find_retained_earnings_account(session, fiscal_year.company_id)
    if retained_account is None:
        raise PeriodActionError(
            "Kein Gewinnvortragskonto gefunden (SKR03: 0860, SKR04: 2970). "
            "Bitte zuerst ein Konto mit Bezeichnung 'Gewinnvortrag' anlegen."
        )

    carryforward_entry = _create_carryforward_entry(
        session=session,
        fiscal_year=fiscal_year,
        retained_account=retained_account,
        changed_by=changed_by,
    )

    periods = (
        session.execute(select(Period).where(Period.fiscal_year_id == fiscal_year.id))
        .scalars()
        .all()
    )
    locked_period_ids = set(
        session.execute(
            select(PeriodLock.period_id).where(
                PeriodLock.period_id.in_([period.id for period in periods])
            )
        ).scalars()
    )

    newly_locked = 0
    for period in periods:
        if period.id in locked_period_ids:
            continue
        session.add(
            PeriodLock(
                tenant_id=period.tenant_id,
                period_id=period.id,
                reason=f"Jahresabschluss {fiscal_year.label}",
                locked_by=changed_by,
            )
        )
        period.status = "locked"
        newly_locked += 1

    fiscal_year.is_closed = True

    log_audit_event(
        session=session,
        tenant_id=fiscal_year.tenant_id,
        company_id=fiscal_year.company_id,
        entity_type="fiscal_year",
        entity_id=str(fiscal_year.id),
        action="closed",
        changed_by=changed_by,
        payload={
            "label": fiscal_year.label,
            "locked_periods": newly_locked,
            "carryforward_posting_number": (
                carryforward_entry.posting_number if carryforward_entry else None
            ),
        },
    )
    session.commit()
    session.refresh(fiscal_year)
    if carryforward_entry is not None:
        session.refresh(carryforward_entry)
    return FiscalYearCloseResult(fiscal_year=fiscal_year, carryforward_entry=carryforward_entry)
