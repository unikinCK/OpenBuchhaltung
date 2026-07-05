from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import Company, FiscalYear, Period, PeriodLock


class PeriodActionError(ValueError):
    """Raised when a period/fiscal-year action is not allowed."""


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


def close_fiscal_year(*, session: Session, fiscal_year_id: int, changed_by: str) -> FiscalYear:
    """Schließt ein Geschäftsjahr: sperrt alle offenen Perioden und markiert es als abgeschlossen.

    Der Ergebnisvortrag ins Folgejahr ist bewusst noch nicht enthalten (Folgeaufgabe);
    die Bilanz weist das Jahresergebnis weiterhin dynamisch aus.
    """
    fiscal_year = session.get(FiscalYear, fiscal_year_id)
    if fiscal_year is None:
        raise PeriodActionError("Geschäftsjahr nicht gefunden.")
    if fiscal_year.is_closed:
        raise PeriodActionError("Das Geschäftsjahr ist bereits abgeschlossen.")

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
        payload={"label": fiscal_year.label, "locked_periods": newly_locked},
    )
    session.commit()
    session.refresh(fiscal_year)
    return fiscal_year
