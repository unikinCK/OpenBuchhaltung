from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta
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
    # Konto entweder direkt per interner ID oder per Kontonummer (``account_code``)
    # angeben. Ist nur die Nummer gesetzt, löst der Service sie zur ID auf.
    account_id: int | None = None
    debit_amount: Decimal = Decimal("0.00")
    credit_amount: Decimal = Decimal("0.00")
    description: str | None = None
    tax_code_id: int | None = None
    account_code: str | None = None


@dataclass(slots=True)
class JournalEntryInput:
    company_id: int
    entry_date: date
    description: str
    status: str
    lines: list[JournalLineInput]
    changed_by: str = "system"
    # Buchung in die Abschlussperiode (Periode 13) statt in die datumsbezogene Monatsperiode.
    post_to_closing_period: bool = False


# Feste Nummer der Abschlussperiode je Wirtschaftsjahr (DATEV-Konvention: Periode 13).
CLOSING_PERIOD_NUMBER = 13
MAX_REGULAR_PERIODS = 12


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def fiscal_year_bounds(start_month: int, dt: date) -> tuple[date, date, str]:
    """Grenzen und Label des regulären Wirtschaftsjahres, das ``dt`` enthält.

    ``start_month`` = 1 entspricht dem Kalenderjahr; jeder andere Wert einem
    abweichenden Wirtschaftsjahr, das im angegebenen Monat beginnt.
    """
    if start_month == 1:
        return date(dt.year, 1, 1), date(dt.year, 12, 31), str(dt.year)

    start_year = dt.year if dt.month >= start_month else dt.year - 1
    end_year = start_year + 1
    end_month = start_month - 1
    start_date = date(start_year, start_month, 1)
    end_date = date(end_year, end_month, _last_day_of_month(end_year, end_month))
    return start_date, end_date, f"{start_year}/{end_year}"


def build_periods_for_fiscal_year(fiscal_year: FiscalYear) -> list[Period]:
    """Erzeugt die Monatsperioden (auf die WJ-Grenzen zugeschnitten) plus Abschlussperiode.

    Für Rumpf- und abweichende Wirtschaftsjahre können die erste und letzte Periode
    Teilmonate sein. Perioden werden fortlaufend ab 1 nummeriert; die Abschlussperiode
    erhält immer die Nummer 13.
    """
    periods: list[Period] = []
    number = 1
    cursor = fiscal_year.start_date
    while cursor <= fiscal_year.end_date:
        month_end = date(cursor.year, cursor.month, _last_day_of_month(cursor.year, cursor.month))
        segment_end = min(month_end, fiscal_year.end_date)
        periods.append(
            Period(
                tenant_id=fiscal_year.tenant_id,
                fiscal_year_id=fiscal_year.id,
                period_number=number,
                start_date=cursor,
                end_date=segment_end,
                status="open",
                is_closing=False,
            )
        )
        number += 1
        if segment_end >= fiscal_year.end_date:
            break
        cursor = month_end + timedelta(days=1)

    if len(periods) > MAX_REGULAR_PERIODS:
        raise ValueError(
            "Ein Wirtschaftsjahr darf höchstens 12 Perioden umfassen. "
            "Bitte den Beginn auf einen Monatsersten legen."
        )

    periods.append(
        Period(
            tenant_id=fiscal_year.tenant_id,
            fiscal_year_id=fiscal_year.id,
            period_number=CLOSING_PERIOD_NUMBER,
            start_date=fiscal_year.end_date,
            end_date=fiscal_year.end_date,
            status="open",
            is_closing=True,
        )
    )
    return periods


def create_journal_entry(*, session: Session, payload: JournalEntryInput) -> JournalEntry:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise JournalEntryCreationError("Gesellschaft nicht gefunden.")

    lines = _resolve_account_codes(session=session, company=company, lines=payload.lines)
    lines = _expand_tax_lines(session=session, company=company, lines=lines)

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
    if fiscal_year.is_closed:
        raise JournalEntryCreationError(
            "Das Geschäftsjahr ist abgeschlossen. Buchung nicht möglich."
        )
    period = _get_or_create_period(
        session=session,
        tenant_id=company.tenant_id,
        fiscal_year_id=fiscal_year.id,
        dt=payload.entry_date,
        closing=payload.post_to_closing_period,
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


def _resolve_account_codes(
    *,
    session: Session,
    company: Company,
    lines: list[JournalLineInput],
) -> list[JournalLineInput]:
    """Ersetzt Kontonummern (``account_code``) durch die interne Konto-ID.

    Zeilen mit gesetzter ``account_id`` bleiben unverändert. Für Zeilen, die nur
    eine Kontonummer tragen, wird das Konto der Gesellschaft anhand der eindeutigen
    Kontonummer nachgeschlagen.
    """
    codes_needed = {
        line.account_code.strip()
        for line in lines
        if line.account_id is None and line.account_code and line.account_code.strip()
    }
    id_by_code: dict[str, int] = {}
    if codes_needed:
        id_by_code = {
            code: account_id
            for account_id, code in session.execute(
                select(Account.id, Account.code).where(
                    Account.company_id == company.id,
                    Account.code.in_(codes_needed),
                )
            ).all()
        }

    resolved: list[JournalLineInput] = []
    for line in lines:
        if line.account_id is not None:
            resolved.append(line)
            continue
        code = (line.account_code or "").strip()
        if not code:
            raise JournalEntryCreationError(
                "Buchungszeile ohne Konto: account_id oder account_code angeben."
            )
        account_id = id_by_code.get(code)
        if account_id is None:
            raise JournalEntryCreationError(
                f"Konto mit Nummer {code} in dieser Gesellschaft nicht gefunden."
            )
        resolved.append(replace(line, account_id=account_id, account_code=code))
    return resolved


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
    # Zuerst ein bereits vorhandenes (auch manuell angelegtes Rumpf-/abweichendes)
    # Wirtschaftsjahr suchen, dessen Zeitraum das Buchungsdatum umfasst.
    fiscal_year = (
        session.execute(
            select(FiscalYear).where(
                FiscalYear.company_id == company_id,
                FiscalYear.start_date <= dt,
                FiscalYear.end_date >= dt,
            )
        )
        .scalars()
        .first()
    )
    if fiscal_year:
        return fiscal_year

    # Sonst ein reguläres Wirtschaftsjahr anhand des Beginn-Monats der Gesellschaft anlegen.
    company = session.get(Company, company_id)
    start_month = company.fiscal_year_start_month if company else 1
    start_date, end_date, label = fiscal_year_bounds(start_month, dt)

    fiscal_year = FiscalYear(
        tenant_id=tenant_id,
        company_id=company_id,
        label=label,
        start_date=start_date,
        end_date=end_date,
        is_closed=False,
    )
    session.add(fiscal_year)
    session.flush()
    for period in build_periods_for_fiscal_year(fiscal_year):
        session.add(period)
    session.flush()
    return fiscal_year


def _get_or_create_period(
    *,
    session: Session,
    tenant_id: int,
    fiscal_year_id: int,
    dt: date,
    closing: bool = False,
) -> Period:
    if closing:
        period = (
            session.execute(
                select(Period).where(
                    Period.fiscal_year_id == fiscal_year_id,
                    Period.is_closing.is_(True),
                )
            )
            .scalars()
            .first()
        )
        if period:
            return period
        fiscal_year = session.get(FiscalYear, fiscal_year_id)
        period = Period(
            tenant_id=tenant_id,
            fiscal_year_id=fiscal_year_id,
            period_number=CLOSING_PERIOD_NUMBER,
            start_date=fiscal_year.end_date,
            end_date=fiscal_year.end_date,
            status="open",
            is_closing=True,
        )
        session.add(period)
        session.flush()
        return period

    # Reguläre Periode, deren Zeitraum das Buchungsdatum enthält.
    period = (
        session.execute(
            select(Period).where(
                Period.fiscal_year_id == fiscal_year_id,
                Period.is_closing.is_(False),
                Period.start_date <= dt,
                Period.end_date >= dt,
            )
        )
        .scalars()
        .first()
    )
    if period:
        return period

    # Fallback für Altbestände ohne vollständig generierte Perioden: fehlenden
    # (Teil-)Monat als reguläre Periode ergänzen. Die Periodennummer ergibt sich
    # – wie in build_periods_for_fiscal_year – deterministisch aus dem
    # Monatsabstand zum WJ-Beginn und bleibt damit im Bereich 1..12 (das
    # Buchungsdatum liegt garantiert innerhalb der WJ-Grenzen). Ein fortlaufendes
    # max_number + 1 könnte dagegen über 13 hinauslaufen und den CHECK-Constraint
    # ck_period_number_range verletzen.
    fiscal_year = session.get(FiscalYear, fiscal_year_id)
    period_number = (
        (dt.year - fiscal_year.start_date.year) * 12
        + (dt.month - fiscal_year.start_date.month)
        + 1
    )
    month_end = date(dt.year, dt.month, _last_day_of_month(dt.year, dt.month))
    period = Period(
        tenant_id=tenant_id,
        fiscal_year_id=fiscal_year_id,
        period_number=period_number,
        start_date=max(date(dt.year, dt.month, 1), fiscal_year.start_date),
        end_date=min(month_end, fiscal_year.end_date),
        status="open",
        is_closing=False,
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
