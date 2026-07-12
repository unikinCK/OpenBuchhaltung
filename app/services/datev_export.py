"""DATEV-Buchungsstapel-Export im EXTF-Format.

Erzeugt eine EXTF-CSV (Format-Kategorie 21 "Buchungsstapel"), die von DATEV
importiert werden kann. Der Aufbau folgt der DATEV-Formatbeschreibung:

    Zeile 1: Kopfzeile (Metadaten zum Stapel)
    Zeile 2: Spaltenüberschriften der Buchungssätze
    Zeile 3+: Buchungssätze

Mapping der internen mehrzeiligen Buchungen auf DATEV:
    * Buchungen mit genau einer Soll- und einer Habenzeile werden als klassische
      Konto/Gegenkonto-Buchung exportiert.
    * Mehrzeilige Buchungen (Splitbuchungen) werden je Position als eigene Zeile
      ohne Gegenkonto exportiert und über das gemeinsame Belegfeld 1
      (Buchungsnummer) gruppiert.

Der Export ist bewusst als "DATEV-kompatibler" Stapel ausgelegt (nicht
zertifiziert): BU-Schlüssel/Steuerautomatik werden nicht gesetzt, da die
Umsatzsteuer bereits als eigene Buchungszeile geführt wird.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import StringIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Account, JournalEntry, JournalEntryLine

EXTF_FORMAT_NAME = "Buchungsstapel"
EXTF_FORMAT_CATEGORY = 21
EXTF_FORMAT_VERSION = 13
EXTF_VERSION = 700

DATA_COLUMNS = (
    "Umsatz (ohne Soll/Haben-Kz)",
    "Soll/Haben-Kennzeichen",
    "WKZ Umsatz",
    "Kurs",
    "Basis-Umsatz",
    "WKZ Basis-Umsatz",
    "Konto",
    "Gegenkonto (ohne BU-Schlüssel)",
    "BU-Schlüssel",
    "Belegdatum",
    "Belegfeld 1",
    "Belegfeld 2",
    "Skonto",
    "Buchungstext",
)


@dataclass(slots=True)
class DatevExportOptions:
    consultant_number: int = 1000  # Beraternummer
    client_number: int = 1  # Mandantennummer
    account_length: int = 4  # Sachkontennummernlänge
    description: str = "OpenBuchhaltung Buchungsstapel"


def _fmt_amount(value: Decimal) -> str:
    """DATEV-Betrag: zwei Nachkommastellen, Komma als Dezimaltrenner, ohne Vorzeichen."""
    return f"{abs(value):.2f}".replace(".", ",")


def _fmt_beleg_date(value: date) -> str:
    """Belegdatum im Format TTMM (das Jahr ergibt sich aus dem Wirtschaftsjahr)."""
    return value.strftime("%d%m")


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _clip(value: str | None, length: int) -> str:
    return (value or "")[:length]


def _header_row(
    *,
    options: DatevExportOptions,
    generated_at: datetime,
    fiscal_year_start: date,
    date_from: date | None,
    date_to: date | None,
    finalized: bool,
) -> str:
    fields = [
        _quote("EXTF"),
        str(EXTF_VERSION),
        str(EXTF_FORMAT_CATEGORY),
        _quote(EXTF_FORMAT_NAME),
        str(EXTF_FORMAT_VERSION),
        generated_at.strftime("%Y%m%d%H%M%S000"),
        "",  # importiert
        _quote("OB"),  # Herkunft
        "",  # exportiert von
        "",  # importiert von
        str(options.consultant_number),
        str(options.client_number),
        fiscal_year_start.strftime("%Y%m%d"),
        str(options.account_length),
        date_from.strftime("%Y%m%d") if date_from else "",
        date_to.strftime("%Y%m%d") if date_to else "",
        _quote(_clip(options.description, 30)),
        "",  # Diktatkürzel
        "1",  # Buchungstyp: 1 = Finanzbuchführung
        "0",  # Rechnungslegungszweck
        # Festschreibung: 1, wenn alle Buchungen des Stapels festgeschrieben sind.
        "1" if finalized else "0",
        _quote("EUR"),
    ]
    return ";".join(fields)


def _booking_row(
    *,
    amount: Decimal,
    debit_credit: str,
    account_code: str,
    contra_account_code: str,
    entry_date: date,
    posting_number: str,
    text: str,
) -> str:
    fields = [
        _fmt_amount(amount),
        _quote(debit_credit),
        _quote("EUR"),
        "",  # Kurs
        "",  # Basis-Umsatz
        "",  # WKZ Basis-Umsatz
        account_code,
        contra_account_code,
        "",  # BU-Schlüssel
        _fmt_beleg_date(entry_date),
        _quote(_clip(posting_number, 36)),
        "",  # Belegfeld 2
        "",  # Skonto
        _quote(_clip(text, 60)),
    ]
    return ";".join(fields)


def build_datev_export(
    *,
    session: Session,
    company_id: int,
    options: DatevExportOptions | None = None,
    generated_at: datetime,
) -> str:
    """Erzeugt den EXTF-Buchungsstapel als String für eine Gesellschaft."""
    options = options or DatevExportOptions()

    entries = (
        session.execute(
            select(JournalEntry)
            .where(JournalEntry.company_id == company_id)
            .order_by(JournalEntry.entry_date, JournalEntry.id)
        )
        .scalars()
        .all()
    )

    line_rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntryLine.line_number,
            Account.code,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.description,
        )
        .join(Account, Account.id == JournalEntryLine.account_id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(JournalEntry.company_id == company_id)
        .order_by(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number)
    ).all()

    lines_by_entry: dict[int, list] = {}
    for row in line_rows:
        lines_by_entry.setdefault(row.journal_entry_id, []).append(row)

    zero = Decimal("0.00")
    dates = [entry.entry_date for entry in entries]
    fiscal_year_start = date(min(dates).year, 1, 1) if dates else date(generated_at.year, 1, 1)

    buffer = StringIO()
    buffer.write(
        _header_row(
            options=options,
            generated_at=generated_at,
            fiscal_year_start=fiscal_year_start,
            date_from=min(dates) if dates else None,
            date_to=max(dates) if dates else None,
            finalized=bool(entries) and all(entry.is_finalized for entry in entries),
        )
    )
    buffer.write("\r\n")
    buffer.write(";".join(_quote(column) for column in DATA_COLUMNS))
    buffer.write("\r\n")

    for entry in entries:
        lines = lines_by_entry.get(entry.id, [])
        debit_lines = [line for line in lines if line.debit_amount > zero]
        credit_lines = [line for line in lines if line.credit_amount > zero]

        if len(debit_lines) == 1 and len(credit_lines) == 1 and len(lines) == 2:
            debit = debit_lines[0]
            credit = credit_lines[0]
            buffer.write(
                _booking_row(
                    amount=debit.debit_amount,
                    debit_credit="S",
                    account_code=debit.code,
                    contra_account_code=credit.code,
                    entry_date=entry.entry_date,
                    posting_number=entry.posting_number,
                    text=entry.description,
                )
            )
            buffer.write("\r\n")
        else:
            # Splitbuchung: je Position eine Zeile ohne Gegenkonto,
            # gruppiert über Belegfeld 1 (Buchungsnummer).
            for line in lines:
                is_debit = line.debit_amount > zero
                buffer.write(
                    _booking_row(
                        amount=line.debit_amount if is_debit else line.credit_amount,
                        debit_credit="S" if is_debit else "H",
                        account_code=line.code,
                        contra_account_code="",
                        entry_date=entry.entry_date,
                        posting_number=entry.posting_number,
                        text=line.description or entry.description,
                    )
                )
                buffer.write("\r\n")

    return buffer.getvalue()
