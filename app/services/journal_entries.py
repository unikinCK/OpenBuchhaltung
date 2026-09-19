from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.compliance_integrity import seal_journal_entry
from app.services.controlling import ControllingError, validate_controlling_assignment
from domain.models import (
    TAX_KIND_INPUT,
    TAX_KIND_OUTPUT,
    Account,
    BankTransaction,
    Company,
    DepreciationEntry,
    FiscalYear,
    FixedAsset,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    PayrollRun,
    Period,
    PeriodLock,
    PostingNumberSequence,
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
    cost_center_id: int | None = None
    profit_center_id: int | None = None


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
    # Herkunft der Buchung (z. B. "manual", "storno").
    source: str = "manual"
    # Gesetzt auf Stornobuchungen: ID der stornierten Originalbuchung.
    reversal_of_id: int | None = None
    # Automatische USt-/VSt-Teilbuchung für Zeilen mit Steuercode. Stornos
    # schalten das ab: sie spiegeln die bereits expandierten Zeilen 1:1 und
    # behalten die Steuercodes nur als Kennzeichnung (z. B. für die UStVA).
    expand_tax_lines: bool = True


# Feste Nummer der Abschlussperiode je Wirtschaftsjahr (DATEV-Konvention: Periode 13).
CLOSING_PERIOD_NUMBER = 13
MAX_REGULAR_PERIODS = 12

# Herkunft (``JournalEntry.source``) von Buchungen, die das System selbst erzeugt.
SOURCE_MANUAL = "manual"
SOURCE_STORNO = "storno"
# Ergebnisvortrag beim Jahresabschluss: Erfolgskonten gegen Gewinnvortrag
# glattgestellt (Abschlussperiode 13).
SOURCE_YEAR_END_CLOSE = "year_end_close"
# Saldovortrag der Bestandskonten ins Folgejahr (EB-Werte, Periode 1).
SOURCE_CARRYFORWARD = "carryforward"
# Vortragsbuchungen: technische Buchungen des Jahresabschlusses, die in GuV,
# UStVA und Ertragsteuern nie Ergebnis oder Umsatz darstellen.
CARRYFORWARD_SOURCES = (SOURCE_YEAR_END_CLOSE, SOURCE_CARRYFORWARD)
# Versuche, eine Buchung mit neuer Nummer anzulegen, wenn die gezogene Nummer
# bereits vergeben ist (Altbestand mit gesellschaftsweitem Zähler, Wettlauf).
POSTING_NUMBER_ATTEMPTS = 5


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


def create_journal_entry(
    *, session: Session, payload: JournalEntryInput, commit: bool = True
) -> JournalEntry:
    """Legt eine Buchung an.

    Mit ``commit=False`` wird nur geflusht — der Aufrufer committet selbst und
    kann so weitere Änderungen (z. B. den Statuswechsel eines Bankumsatzes)
    atomar mit der Buchung zusammenfassen.
    """
    company = session.get(Company, payload.company_id)
    if company is None:
        raise JournalEntryCreationError("Gesellschaft nicht gefunden.")

    lines = _resolve_account_codes(session=session, company=company, lines=payload.lines)
    tax_codes = _load_tax_codes(session=session, company=company, lines=lines)
    if payload.expand_tax_lines:
        lines = _expand_tax_lines(lines=lines, tax_codes=tax_codes)

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
        try:
            validate_controlling_assignment(
                session=session,
                company_id=company.id,
                entry_date=payload.entry_date,
                unit_id=line.cost_center_id,
                expected_type="cost_center",
            )
            validate_controlling_assignment(
                session=session,
                company_id=company.id,
                entry_date=payload.entry_date,
                unit_id=line.profit_center_id,
                expected_type="profit_center",
            )
        except ControllingError as exc:
            raise JournalEntryCreationError(str(exc)) from exc

    _validate_tax_code_usage(lines=lines, accounts=accounts, tax_codes=tax_codes)

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

    for attempt in range(1, POSTING_NUMBER_ATTEMPTS + 1):
        posting_number = _next_posting_number(session=session, fiscal_year=fiscal_year)
        entry = JournalEntry(
            tenant_id=company.tenant_id,
            company_id=company.id,
            fiscal_year_id=fiscal_year.id,
            period_id=period.id,
            posting_number=posting_number,
            entry_date=payload.entry_date,
            description=payload.description,
            source=payload.source,
            reversal_of_id=payload.reversal_of_id,
        )
        try:
            with session.begin_nested():
                session.add(entry)
                session.flush()
        except IntegrityError as exc:
            if not _is_posting_number_conflict(exc) or attempt == POSTING_NUMBER_ATTEMPTS:
                raise
            # Nummer bereits vergeben (Altbestand/Wettlauf): nächste Nummer ziehen.
            continue
        break

    for idx, line in enumerate(lines, start=1):
        line_payload = {
            "tenant_id": company.tenant_id,
            "journal_entry_id": entry.id,
            "line_number": idx,
            "account_id": line.account_id,
            "description": line.description,
            "currency_code": company.currency_code,
            "tax_code_id": line.tax_code_id,
            "cost_center_id": line.cost_center_id,
            "profit_center_id": line.profit_center_id,
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

    if commit:
        session.commit()
        session.refresh(entry)
    else:
        session.flush()
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


def _load_tax_codes(
    *, session: Session, company: Company, lines: list[JournalLineInput]
) -> dict[int, TaxCode]:
    """Steuercodes der Gesellschaft, die in den Zeilen referenziert werden."""
    tax_code_ids = {line.tax_code_id for line in lines if line.tax_code_id is not None}
    if not tax_code_ids:
        return {}
    return {
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


# Kontoarten, auf denen die Bemessungsgrundlage eines Steuercodes liegen darf:
# Vorsteuer entsteht aus Aufwand und Anlagenzugängen, Umsatzsteuer aus Erlösen
# (und erhaltenen Anzahlungen auf Verbindlichkeitskonten).
_ALLOWED_BASE_ACCOUNT_TYPES = {
    TAX_KIND_INPUT: {"expense", "asset"},
    TAX_KIND_OUTPUT: {"revenue", "income", "liability"},
}
_TAX_KIND_HINTS = {
    TAX_KIND_INPUT: "Vorsteuer gehört auf Aufwands- oder Anlagenkonten",
    TAX_KIND_OUTPUT: "Umsatzsteuer gehört auf Erlös- bzw. Ertragskonten",
}


def _validate_tax_code_usage(
    *,
    lines: list[JournalLineInput],
    accounts: dict[int, Account],
    tax_codes: dict[int, TaxCode],
) -> None:
    """Prüft die Richtung der Steuercodes gegen Kontoart und Soll-/Haben-Seite.

    Ein Vorsteuercode auf einer Erlöszeile würde die Vorsteuer im Haben mindern
    statt Umsatzsteuer auszuweisen (Kz 66 sinkt statt Kz 81 steigt). Explizite
    Steuerzeilen (Zeile auf dem Steuerkonto des Codes, z. B. Storno oder
    Netto-aus-Brutto) müssen auf derselben Seite stehen wie ihre
    Bemessungsgrundlage. Codes mit 0 % erzeugen keine Steuer und werden nicht
    gegen die Kontoart geprüft.
    """
    base_sides: dict[int, set[str]] = {}
    tax_lines: list[tuple[TaxCode, str]] = []
    for line in lines:
        if line.tax_code_id is None:
            continue
        tax_code = tax_codes.get(line.tax_code_id)
        if tax_code is None:
            raise JournalEntryCreationError("Steuercode für Buchungszeile nicht gefunden.")
        side = "debit" if line.debit_amount > Decimal("0.00") else "credit"
        if tax_code.vat_account_id is not None and line.account_id == tax_code.vat_account_id:
            tax_lines.append((tax_code, side))
            continue
        base_sides.setdefault(tax_code.id, set()).add(side)
        if tax_code.rate == Decimal("0.00"):
            continue
        account = accounts[line.account_id]
        if account.account_type not in _ALLOWED_BASE_ACCOUNT_TYPES.get(tax_code.kind, set()):
            label = "Vorsteuer" if tax_code.kind == TAX_KIND_INPUT else "Umsatzsteuer"
            raise JournalEntryCreationError(
                f"Steuercode {tax_code.code} ({label}) ist auf Konto {account.code} "
                f"(Kontoart {account.account_type}) nicht zulässig — "
                f"{_TAX_KIND_HINTS.get(tax_code.kind, '')}."
            )
    for tax_code, side in tax_lines:
        sides = base_sides.get(tax_code.id)
        if sides and side not in sides:
            raise JournalEntryCreationError(
                f"Die Steuerzeile zu {tax_code.code} muss auf derselben Seite (Soll/Haben) "
                "stehen wie ihre Bemessungsgrundlage."
            )


def _expand_tax_lines(
    *,
    lines: list[JournalLineInput],
    tax_codes: dict[int, TaxCode],
) -> list[JournalLineInput]:
    """Erzeugt für Zeilen mit Steuercode die automatische USt-/VSt-Teilbuchung.

    Beträge der Ursprungszeile gelten als Netto; die Steuerzeile wird auf derselben
    Seite (Soll/Haben) auf dem Steuerkonto des Steuercodes ergänzt.
    """
    if not tax_codes:
        return list(lines)

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
                cost_center_id=line.cost_center_id,
                profit_center_id=line.profit_center_id,
            )
        )

    return expanded


def parse_decimal(value: str, *, places: int | None = 2) -> Decimal:
    """Parst einen Dezimalstring.

    Mit ``places`` (Standard 2 = Cent) werden Werte mit mehr Nachkommastellen
    abgelehnt statt stillschweigend gerundet (0,015 + 0,015 wäre sonst als
    0,03 buchbar); das Ergebnis ist auf ``places`` Stellen quantisiert
    (``"5"`` → ``5.00``). ``places=None`` parst ohne Stellenprüfung, z. B. für
    Prozentsätze, AfA-Sätze oder Mengen.
    """
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, AttributeError):
        raise JournalEntryCreationError("Betrag ist keine gültige Dezimalzahl.") from None
    if not parsed.is_finite():
        raise JournalEntryCreationError("Betrag ist keine gültige Dezimalzahl.")
    if places is None:
        return parsed
    quantum = Decimal(1).scaleb(-places)
    try:
        quantized = parsed.quantize(quantum)
    except InvalidOperation:
        raise JournalEntryCreationError("Betrag ist zu groß.") from None
    if quantized != parsed:
        raise JournalEntryCreationError(
            f"Betrag darf höchstens {places} Nachkommastellen haben ({parsed})."
        )
    return quantized


def get_or_create_fiscal_year(
    *, session: Session, tenant_id: int, company_id: int, dt: date
) -> FiscalYear:
    """Geschäftsjahr zum Datum — vorhanden oder regulär angelegt (siehe unten)."""
    return _get_or_create_fiscal_year(
        session=session, tenant_id=tenant_id, company_id=company_id, dt=dt
    )


def _get_or_create_fiscal_year(
    *,
    session: Session,
    tenant_id: int,
    company_id: int,
    dt: date,
) -> FiscalYear:
    # Zuerst ein bereits vorhandenes (auch manuell angelegtes Rumpf-/abweichendes)
    # Wirtschaftsjahr suchen, dessen Zeitraum das Buchungsdatum umfasst.
    fiscal_year = _find_fiscal_year_containing(session=session, company_id=company_id, dt=dt)
    if fiscal_year:
        return fiscal_year

    # Sonst ein reguläres Wirtschaftsjahr anhand des Beginn-Monats der Gesellschaft anlegen.
    company = session.get(Company, company_id)
    start_month = company.fiscal_year_start_month if company else 1
    start_date, end_date, label = fiscal_year_bounds(start_month, dt)

    # Das automatische Jahr darf sich nicht mit manuell angelegten (Rumpf-/
    # abweichenden) Geschäftsjahren überschneiden — sonst entstünden zwei
    # Jahre für dasselbe Datum und die Zuordnung wäre Zufall.
    overlapping = (
        session.execute(
            select(FiscalYear.label)
            .where(
                FiscalYear.company_id == company_id,
                FiscalYear.start_date <= end_date,
                FiscalYear.end_date >= start_date,
            )
            .order_by(FiscalYear.start_date)
        )
        .scalars()
        .all()
    )
    if overlapping:
        raise JournalEntryCreationError(
            f"Für das Buchungsdatum {dt.isoformat()} existiert kein Geschäftsjahr; das "
            f"automatische Geschäftsjahr {label} ({start_date.isoformat()} – "
            f"{end_date.isoformat()}) würde sich mit {', '.join(overlapping)} überschneiden. "
            "Bitte das passende Geschäftsjahr in der Periodenverwaltung manuell anlegen."
        )

    fiscal_year = FiscalYear(
        tenant_id=tenant_id,
        company_id=company_id,
        label=label,
        start_date=start_date,
        end_date=end_date,
        is_closed=False,
    )
    try:
        with session.begin_nested():
            session.add(fiscal_year)
            session.flush()
            for period in build_periods_for_fiscal_year(fiscal_year):
                session.add(period)
    except IntegrityError:
        # Wettlauf: Eine parallele Buchung hat das Geschäftsjahr inzwischen
        # angelegt (UNIQUE auf Bezeichnung) — erneut nachschlagen.
        fiscal_year = _find_fiscal_year_containing(
            session=session, company_id=company_id, dt=dt
        )
        if fiscal_year is None:
            raise JournalEntryCreationError(
                f"Das Geschäftsjahr {label} konnte nicht angelegt werden (Bezeichnung "
                "bereits vergeben). Bitte das Geschäftsjahr manuell anlegen."
            ) from None
        return fiscal_year
    session.flush()
    return fiscal_year


def _find_fiscal_year_containing(
    *, session: Session, company_id: int, dt: date
) -> FiscalYear | None:
    """Das Geschäftsjahr, dessen Zeitraum ``dt`` enthält (eindeutig oder Fehler)."""
    matches = (
        session.execute(
            select(FiscalYear)
            .where(
                FiscalYear.company_id == company_id,
                FiscalYear.start_date <= dt,
                FiscalYear.end_date >= dt,
            )
            .order_by(FiscalYear.start_date, FiscalYear.id)
        )
        .scalars()
        .all()
    )
    if len(matches) > 1:
        labels = ", ".join(match.label for match in matches)
        raise JournalEntryCreationError(
            f"Mehrere Geschäftsjahre enthalten das Buchungsdatum {dt.isoformat()} "
            f"({labels}). Bitte die Geschäftsjahre in der Periodenverwaltung korrigieren."
        )
    return matches[0] if matches else None


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
        # Altbestände können die Nummer 13 versehentlich als reguläre Periode
        # belegen; dann würde der Insert am UNIQUE-Constraint
        # uq_period_fiscal_year_number scheitern.
        number_taken = session.execute(
            select(Period.id).where(
                Period.fiscal_year_id == fiscal_year_id,
                Period.period_number == CLOSING_PERIOD_NUMBER,
            )
        ).first()
        if number_taken:
            raise JournalEntryCreationError(
                "Die Abschlussperiode kann nicht angelegt werden: Die Periodennummer "
                f"{CLOSING_PERIOD_NUMBER} ist bereits durch eine reguläre Periode belegt. "
                "Bitte die Perioden des Wirtschaftsjahres korrigieren."
            )
        period = Period(
            tenant_id=tenant_id,
            fiscal_year_id=fiscal_year_id,
            period_number=CLOSING_PERIOD_NUMBER,
            start_date=fiscal_year.end_date,
            end_date=fiscal_year.end_date,
            status="open",
            is_closing=True,
        )
        if _insert_period(session=session, period=period):
            return period
        # Wettlauf: Abschlussperiode wurde parallel angelegt — erneut nachschlagen.
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
        if period is None:
            raise JournalEntryCreationError(
                "Die Abschlussperiode konnte nicht angelegt werden. Bitte erneut versuchen."
            )
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
    # (Teil-)Monat als reguläre Periode ergänzen. Bevorzugt wird – wie in
    # build_periods_for_fiscal_year – die deterministische Nummer aus dem
    # Monatsabstand zum WJ-Beginn. Bei Altbeständen kann diese Nummer jedoch
    # bereits durch eine anders zugeschnittene Periode belegt sein (frühere
    # Versionen nummerierten in Buchungsreihenfolge per max + 1); dann wird die
    # kleinste freie Nummer 1..12 verwendet, damit der UNIQUE-Constraint
    # uq_period_fiscal_year_number nicht verletzt wird.
    fiscal_year = session.get(FiscalYear, fiscal_year_id)
    taken_numbers = set(
        session.execute(
            select(Period.period_number).where(Period.fiscal_year_id == fiscal_year_id)
        ).scalars()
    )
    preferred_number = (
        (dt.year - fiscal_year.start_date.year) * 12
        + (dt.month - fiscal_year.start_date.month)
        + 1
    )
    period_number = next(
        (
            number
            for number in [preferred_number, *range(1, MAX_REGULAR_PERIODS + 1)]
            if 1 <= number <= MAX_REGULAR_PERIODS and number not in taken_numbers
        ),
        None,
    )
    if period_number is None:
        raise JournalEntryCreationError(
            "Für das Buchungsdatum existiert keine passende Periode und alle "
            "Periodennummern 1–12 sind bereits vergeben. Bitte die Perioden des "
            "Wirtschaftsjahres in der Periodenverwaltung prüfen."
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
    if _insert_period(session=session, period=period):
        return period
    # Wettlauf: Eine parallele Buchung hat die Periode inzwischen angelegt —
    # erneut nach dem Zeitraum suchen.
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
    if period is None:
        raise JournalEntryCreationError(
            "Die Buchungsperiode konnte nicht angelegt werden. Bitte erneut versuchen."
        )
    return period


def _insert_period(*, session: Session, period: Period) -> bool:
    """Legt eine Periode über einen Savepoint an.

    Gibt ``False`` zurück, wenn der UNIQUE-Constraint verletzt wurde (die Periode
    wurde parallel angelegt); die Session bleibt dabei benutzbar.
    """
    try:
        with session.begin_nested():
            session.add(period)
    except IntegrityError:
        return False
    return True


def posting_number_prefix(fiscal_year: FiscalYear) -> str:
    """Präfix der Buchungsnummern eines Geschäftsjahres (z. B. ``2026``, ``2026/2027``).

    Freie Bezeichnungen werden auf Ziffern, Buchstaben und ``/`` reduziert
    (``2026 (Rumpf)`` → ``2026-Rumpf``), damit die Nummer DATEV-tauglich bleibt.
    """
    label = re.sub(r"[^0-9A-Za-z/]+", "-", fiscal_year.label.strip()).strip("-")
    return label or str(fiscal_year.end_date.year)


def _max_existing_posting_number(*, session: Session, company_id: int, prefix: str) -> int:
    """Höchste bereits vergebene laufende Nummer mit diesem Präfix (Altbestand).

    Vor Einführung des Nummernkreises je Geschäftsjahr wurde gesellschaftsweit
    gezählt; der neue Zähler startet oberhalb der vorhandenen Nummern.
    """
    posting_numbers = session.execute(
        select(JournalEntry.posting_number).where(
            JournalEntry.company_id == company_id,
            JournalEntry.posting_number.like(f"{prefix}-%"),
        )
    ).scalars()
    highest = 0
    for posting_number in posting_numbers:
        suffix = posting_number[len(prefix) + 1 :]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest


def _next_posting_number(*, session: Session, fiscal_year: FiscalYear) -> str:
    """Zieht die nächste Buchungsnummer aus dem Nummernkreis des Geschäftsjahres.

    Die Sequenzzeile wird mit ``FOR UPDATE`` gesperrt (PostgreSQL), sodass
    parallele Buchungen nacheinander nummeriert werden. Fehlt die Zeile noch,
    wird sie oberhalb vorhandener Altnummern angelegt; legt eine parallele
    Transaktion sie gleichzeitig an, gewinnt der UNIQUE-Constraint und die
    Zeile wird erneut gesperrt gelesen.
    """
    prefix = posting_number_prefix(fiscal_year)
    sequence_stmt = (
        select(PostingNumberSequence)
        .where(PostingNumberSequence.fiscal_year_id == fiscal_year.id)
        .with_for_update()
    )
    sequence = session.execute(sequence_stmt).scalar_one_or_none()
    if sequence is None:
        sequence = PostingNumberSequence(
            tenant_id=fiscal_year.tenant_id,
            company_id=fiscal_year.company_id,
            fiscal_year_id=fiscal_year.id,
            last_number=_max_existing_posting_number(
                session=session, company_id=fiscal_year.company_id, prefix=prefix
            ),
        )
        try:
            with session.begin_nested():
                session.add(sequence)
        except IntegrityError:
            sequence = session.execute(sequence_stmt).scalar_one()
    sequence.last_number += 1
    session.flush()
    return f"{prefix}-{sequence.last_number:04d}"


def _is_posting_number_conflict(exc: IntegrityError) -> bool:
    message = str(exc.orig if exc.orig is not None else exc)
    return "posting_number" in message or "uq_journal_entry_company_no" in message


def _ensure_period_is_open(*, session: Session, period_id: int) -> None:
    locked = session.execute(select(PeriodLock.id).where(PeriodLock.period_id == period_id)).first()
    if locked:
        raise JournalEntryCreationError("Die Periode ist gesperrt. Buchung nicht möglich.")


def finalize_journal_entry(
    *,
    session: Session,
    journal_entry_id: int,
    changed_by: str,
) -> JournalEntry:
    """Schreibt eine Buchung fest (GoBD): danach ist sie unveränderbar.

    Korrekturen an festgeschriebenen Buchungen sind nur noch über
    ``reverse_journal_entry`` (Stornobuchung) möglich.
    """
    entry = session.execute(
        select(JournalEntry)
        .where(JournalEntry.id == journal_entry_id)
        .options(selectinload(JournalEntry.lines))
        .with_for_update()
    ).scalar_one_or_none()
    if entry is None:
        raise JournalEntryCreationError("Buchung nicht gefunden.")
    if entry.is_finalized:
        raise JournalEntryCreationError(
            f"Buchung {entry.posting_number} ist bereits festgeschrieben."
        )

    seal_journal_entry(
        entry,
        finalized_at=datetime.now(timezone.utc),
        finalized_by=changed_by,
    )

    log_audit_event(
        session=session,
        tenant_id=entry.tenant_id,
        company_id=entry.company_id,
        entity_type="journal_entry",
        entity_id=str(entry.id),
        action="finalized",
        changed_by=changed_by,
        payload={"posting_number": entry.posting_number},
    )
    session.commit()
    session.refresh(entry)
    return entry


def finalize_journal_entries_until(
    *,
    session: Session,
    company_id: int,
    up_to_date: date,
    changed_by: str,
) -> int:
    """Festschreibelauf: schreibt alle offenen Buchungen bis einschließlich Datum fest.

    Gibt die Anzahl der festgeschriebenen Buchungen zurück.
    """
    entries = (
        session.execute(
            select(JournalEntry)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.is_finalized.is_(False),
                JournalEntry.entry_date <= up_to_date,
            )
            .options(selectinload(JournalEntry.lines))
            .order_by(JournalEntry.entry_date, JournalEntry.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    if not entries:
        return 0

    finalized_at = datetime.now(timezone.utc)
    for entry in entries:
        seal_journal_entry(entry, finalized_at=finalized_at, finalized_by=changed_by)
        log_audit_event(
            session=session,
            tenant_id=entry.tenant_id,
            company_id=entry.company_id,
            entity_type="journal_entry",
            entity_id=str(entry.id),
            action="finalized",
            changed_by=changed_by,
            payload={
                "posting_number": entry.posting_number,
                "bulk_up_to_date": up_to_date.isoformat(),
            },
        )
    session.commit()
    return len(entries)


def reverse_journal_entry(
    *,
    session: Session,
    journal_entry_id: int,
    reversal_date: date,
    changed_by: str,
) -> JournalEntry:
    """Storniert eine Buchung über eine Gegenbuchung (GoBD-Storno-Prinzip).

    Die Originalbuchung bleibt unverändert; die Stornobuchung spiegelt alle
    Zeilen (Soll/Haben vertauscht) und verweist über ``reversal_of_id`` auf das
    Original. Die Stornobuchung wird sofort festgeschrieben.

    Nebenbücher werden mitgezogen (Storno-Hooks): ein aus der Buchung
    entstandener Bankumsatz geht zurück auf „open“, ein AfA-Satz wird
    zurückgenommen (Buchwert und Anlagenstatus folgen dem Hauptbuch), ein
    gebuchter Lohnlauf wird wieder zum Entwurf und ein mit dieser Buchung
    ausgeglichener offener Posten lebt wieder auf. Alles in einer Transaktion.
    """
    original = session.get(JournalEntry, journal_entry_id)
    if original is None:
        raise JournalEntryCreationError("Buchung nicht gefunden.")
    if original.reversal_of_id is not None:
        raise JournalEntryCreationError(
            f"Buchung {original.posting_number} ist selbst eine Stornobuchung "
            "und kann nicht storniert werden."
        )
    if reversal_date < original.entry_date:
        raise JournalEntryCreationError(
            f"Das Stornodatum {reversal_date.isoformat()} darf nicht vor dem "
            f"Buchungsdatum {original.entry_date.isoformat()} der Buchung "
            f"{original.posting_number} liegen."
        )
    if original.source in CARRYFORWARD_SOURCES:
        raise JournalEntryCreationError(
            f"Buchung {original.posting_number} ist eine Vortragsbuchung des "
            "Jahresabschlusses und kann nicht storniert werden."
        )
    already_reversed = session.execute(
        select(JournalEntry.posting_number).where(
            JournalEntry.reversal_of_id == original.id
        )
    ).first()
    if already_reversed:
        raise JournalEntryCreationError(
            f"Buchung {original.posting_number} wurde bereits storniert "
            f"({already_reversed.posting_number})."
        )

    original_period = session.get(Period, original.period_id)

    # Zeilen gespiegelt übernehmen (inkl. Steuercode als Kennzeichnung).
    # Die Steuerzeilen des Originals sind bereits enthalten; die Auto-Expansion
    # wird deshalb über expand_tax_lines=False abgeschaltet.
    mirrored_lines = [
        JournalLineInput(
            account_id=line.account_id,
            debit_amount=line.credit_amount,
            credit_amount=line.debit_amount,
            description=line.description,
            tax_code_id=line.tax_code_id,
            cost_center_id=line.cost_center_id,
            profit_center_id=line.profit_center_id,
        )
        for line in sorted(original.lines, key=lambda line: line.line_number)
    ]

    reversal = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=original.company_id,
            entry_date=reversal_date,
            description=f"Storno {original.posting_number}: {original.description}",
            status="posted",
            changed_by=changed_by,
            lines=mirrored_lines,
            post_to_closing_period=bool(original_period and original_period.is_closing),
            source="storno",
            reversal_of_id=original.id,
            expand_tax_lines=False,
        ),
        # Stornobuchung, Festschreibung und Audit werden gemeinsam committet.
        commit=False,
    )

    # Stornobuchungen sind Korrekturbelege und werden sofort festgeschrieben.
    seal_journal_entry(
        reversal,
        finalized_at=datetime.now(timezone.utc),
        finalized_by=changed_by,
    )

    released = _release_subledgers(
        session=session, original=original, reversal=reversal, changed_by=changed_by
    )

    log_audit_event(
        session=session,
        tenant_id=original.tenant_id,
        company_id=original.company_id,
        entity_type="journal_entry",
        entity_id=str(original.id),
        action="reversed",
        changed_by=changed_by,
        payload={
            "posting_number": original.posting_number,
            "reversal_posting_number": reversal.posting_number,
            "reversal_entry_id": reversal.id,
            "reversal_date": reversal_date.isoformat(),
            "subledgers": released,
        },
    )
    session.commit()
    session.refresh(reversal)
    return reversal


def _release_subledgers(
    *, session: Session, original: JournalEntry, reversal: JournalEntry, changed_by: str
) -> dict[str, list[int]]:
    """Storno-Hooks: Nebenbücher an den Storno der Originalbuchung anpassen.

    Gibt je Nebenbuch die IDs der angepassten Datensätze zurück (für das
    Audit-Payload). Kein eigener Commit — läuft in der Storno-Transaktion.
    """
    released: dict[str, list[int]] = {}
    audit_common = {
        "session": session,
        "tenant_id": original.tenant_id,
        "company_id": original.company_id,
        "changed_by": changed_by,
    }
    link = {
        "journal_entry_id": original.id,
        "posting_number": original.posting_number,
        "reversal_entry_id": reversal.id,
        "reversal_posting_number": reversal.posting_number,
    }

    # Bank: verbuchter (oder per OPOS zugeordneter) Umsatz wieder offen.
    transactions = (
        session.execute(
            select(BankTransaction).where(BankTransaction.journal_entry_id == original.id)
        )
        .scalars()
        .all()
    )
    for transaction in transactions:
        previous_status = transaction.status
        transaction.status = "open"
        transaction.journal_entry_id = None
        log_audit_event(
            **audit_common,
            entity_type="bank_transaction",
            entity_id=str(transaction.id),
            action="unbooked",
            payload={**link, "previous_status": previous_status},
        )
        released.setdefault("bank_transactions", []).append(transaction.id)

    # Anlagen: AfA-/Abgangssatz zurücknehmen, damit Buchwert = Hauptbuch.
    depreciation_entries = (
        session.execute(
            select(DepreciationEntry).where(DepreciationEntry.journal_entry_id == original.id)
        )
        .scalars()
        .all()
    )
    for depreciation in depreciation_entries:
        asset = session.get(FixedAsset, depreciation.fixed_asset_id)
        payload = {
            **link,
            "depreciation_entry_id": depreciation.id,
            "fiscal_year": depreciation.fiscal_year,
            "kind": depreciation.kind,
            "amount": str(depreciation.amount),
        }
        session.delete(depreciation)
        if asset is not None:
            if depreciation.kind == "abgang":
                asset.disposal_date = None
                asset.disposal_proceeds = None
            if asset.status in {"fully_depreciated", "disposed"}:
                asset.status = "active"
            log_audit_event(
                **audit_common,
                entity_type="fixed_asset",
                entity_id=str(asset.id),
                action="depreciation_reversed",
                payload={**payload, "status": asset.status},
            )
        released.setdefault("depreciation_entries", []).append(depreciation.id)

    # Lohn: gebuchter Lauf zurück auf Entwurf (kann erneut gebucht werden).
    payroll_runs = (
        session.execute(select(PayrollRun).where(PayrollRun.journal_entry_id == original.id))
        .scalars()
        .all()
    )
    for run in payroll_runs:
        run.status = "draft"
        run.journal_entry_id = None
        run.posted_at = None
        log_audit_event(
            **audit_common,
            entity_type="payroll_run",
            entity_id=str(run.id),
            action="unposted",
            payload={**link, "period_label": run.period_label},
        )
        released.setdefault("payroll_runs", []).append(run.id)

    # OPOS: mit dieser Buchung ausgeglichene Posten leben wieder auf.
    open_items = (
        session.execute(
            select(OpenItem).where(OpenItem.settlement_journal_entry_id == original.id)
        )
        .scalars()
        .all()
    )
    for item in open_items:
        restored = item.settlement_amount or (item.original_amount - item.open_amount)
        item.open_amount = min(
            item.original_amount, (item.open_amount + restored).quantize(Decimal("0.01"))
        )
        item.status = "open"
        item.settled_at = None
        item.settled_by = None
        item.settlement_journal_entry_id = None
        item.settlement_amount = None
        log_audit_event(
            **audit_common,
            entity_type="open_item",
            entity_id=str(item.id),
            action="reopened",
            payload={
                **link,
                "restored_amount": str(restored),
                "open_amount": str(item.open_amount),
            },
        )
        released.setdefault("open_items", []).append(item.id)

    session.flush()
    return released
