from __future__ import annotations

import csv
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import (
    Account,
    BankTransaction,
    Company,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)

logger = logging.getLogger(__name__)

REQUIRED_FIELDS = ("booking_date", "amount", "purpose")
CSV_FIELD_ALIASES = {
    "booking_date": ("booking_date", "buchungstag", "datum", "date", "valuta", "wertstellung"),
    "amount": ("amount", "betrag", "umsatz", "betrag_eur"),
    "purpose": ("purpose", "verwendungszweck", "beschreibung", "description", "buchungstext"),
    "counterparty": (
        "counterparty",
        "name",
        "auftraggeber/empfänger",
        "auftraggeber",
        "empfänger",
        "beguenstigter/zahlungspflichtiger",
    ),
}
DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y")


class BankImportError(ValueError):
    """Raised when a bank import or transaction action is invalid."""


@dataclass
class BankImportRowError:
    line_number: int
    message: str


@dataclass
class BankImportReport:
    total_rows: int = 0
    imported_rows: int = 0
    duplicate_rows: int = 0
    error_rows: int = 0
    errors: list[BankImportRowError] = field(default_factory=list)


def import_bank_csv(
    *,
    session: Session,
    company_id: int,
    bank_account_id: int,
    csv_stream: TextIO,
    changed_by: str,
) -> BankImportReport:
    """Importiert Bankumsätze aus einem CSV-Stream (idempotent über Dedup-Hash)."""
    company = session.get(Company, company_id)
    if company is None:
        raise BankImportError("Gesellschaft nicht gefunden.")

    bank_account = session.get(Account, bank_account_id)
    if bank_account is None or bank_account.company_id != company.id:
        raise BankImportError("Bankkonto nicht gefunden.")

    reader = csv.DictReader(csv_stream, delimiter=_sniff_delimiter(csv_stream))
    if not reader.fieldnames:
        raise BankImportError("CSV-Kopfzeile fehlt.")

    header_mapping = _resolve_header_mapping(reader.fieldnames)
    report = BankImportReport()

    existing_hashes = set(
        session.execute(
            select(BankTransaction.dedup_hash).where(BankTransaction.company_id == company.id)
        ).scalars()
    )

    for line_number, raw_row in enumerate(reader, start=2):
        report.total_rows += 1
        row = {
            canonical: (raw_row.get(source) or "").strip()
            for canonical, source in header_mapping.items()
        }

        missing = [name for name in REQUIRED_FIELDS if not row.get(name)]
        if missing:
            _record_error(report, line_number, f"Pflichtfelder fehlen: {', '.join(missing)}")
            continue

        try:
            booking_date = _parse_date(row["booking_date"])
            amount = _parse_amount(row["amount"])
        except BankImportError as exc:
            _record_error(report, line_number, str(exc))
            continue

        if amount == Decimal("0.00"):
            _record_error(report, line_number, "Betrag darf nicht 0 sein.")
            continue

        counterparty = row.get("counterparty") or None
        dedup_hash = _dedup_hash(
            bank_account_id=bank_account.id,
            booking_date=booking_date,
            amount=amount,
            purpose=row["purpose"],
            counterparty=counterparty,
        )
        if dedup_hash in existing_hashes:
            report.duplicate_rows += 1
            continue

        session.add(
            BankTransaction(
                tenant_id=company.tenant_id,
                company_id=company.id,
                bank_account_id=bank_account.id,
                booking_date=booking_date,
                amount=amount,
                currency_code=company.currency_code,
                purpose=row["purpose"],
                counterparty=counterparty,
                dedup_hash=dedup_hash,
            )
        )
        existing_hashes.add(dedup_hash)
        report.imported_rows += 1

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="bank_import",
        entity_id=str(bank_account.id),
        action="imported",
        changed_by=changed_by,
        payload={
            "imported": report.imported_rows,
            "duplicates": report.duplicate_rows,
            "errors": report.error_rows,
        },
    )
    session.commit()
    return report


def suggest_matches(
    *, session: Session, transaction: BankTransaction, limit: int = 5
) -> list[JournalEntry]:
    """Buchungen mit passender Zeile auf dem Bankkonto (Betrag + Seite), noch unverknüpft."""
    line_filter = (
        JournalEntryLine.debit_amount == transaction.amount
        if transaction.amount > 0
        else JournalEntryLine.credit_amount == -transaction.amount
    )
    already_linked = select(BankTransaction.journal_entry_id).where(
        BankTransaction.journal_entry_id.is_not(None)
    )
    return (
        session.execute(
            select(JournalEntry)
            .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
            .where(
                JournalEntry.company_id == transaction.company_id,
                JournalEntryLine.account_id == transaction.bank_account_id,
                line_filter,
                JournalEntry.id.not_in(already_linked),
            )
            .order_by(JournalEntry.entry_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def match_transaction(
    *, session: Session, transaction_id: int, journal_entry_id: int, changed_by: str
) -> BankTransaction:
    transaction = session.get(BankTransaction, transaction_id)
    if transaction is None:
        raise BankImportError("Bankumsatz nicht gefunden.")
    if transaction.status != "open":
        raise BankImportError("Der Bankumsatz ist bereits zugeordnet.")

    entry = session.get(JournalEntry, journal_entry_id)
    if entry is None or entry.company_id != transaction.company_id:
        raise BankImportError("Buchung nicht gefunden.")

    transaction.journal_entry_id = entry.id
    transaction.status = "matched"

    log_audit_event(
        session=session,
        tenant_id=transaction.tenant_id,
        company_id=transaction.company_id,
        entity_type="bank_transaction",
        entity_id=str(transaction.id),
        action="matched",
        changed_by=changed_by,
        payload={"journal_entry_id": entry.id, "posting_number": entry.posting_number},
    )
    session.commit()
    session.refresh(transaction)
    return transaction


def net_from_gross(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """Zerlegt einen Bruttobetrag in Netto + Steuer, konsistent zur Steuer-Expansion.

    Sucht den Nettobetrag, dessen gerundete Steuer exakt zum Brutto aufsummiert.
    """
    if rate <= 0:
        return gross, Decimal("0.00")

    cent = Decimal("0.01")
    base = (gross / (Decimal("1") + rate / Decimal("100"))).quantize(cent, rounding=ROUND_HALF_UP)
    for offset in (Decimal("0.00"), -cent, cent, -2 * cent, 2 * cent):
        net = base + offset
        tax = (net * rate / Decimal("100")).quantize(cent, rounding=ROUND_HALF_UP)
        if net + tax == gross:
            return net, tax
    raise BankImportError(
        f"Bruttobetrag {gross} lässt sich nicht sauber in Netto + {rate}% Steuer zerlegen."
    )


def book_transaction(
    *,
    session: Session,
    transaction_id: int,
    contra_account_id: int,
    changed_by: str,
    tax_code_id: int | None = None,
    description: str | None = None,
) -> BankTransaction:
    """Erzeugt aus einem offenen Bankumsatz eine Buchung (Bankkonto gegen Gegenkonto)."""
    transaction = session.get(BankTransaction, transaction_id)
    if transaction is None:
        raise BankImportError("Bankumsatz nicht gefunden.")
    if transaction.status != "open":
        raise BankImportError("Der Bankumsatz ist bereits zugeordnet.")

    contra_account = session.get(Account, contra_account_id)
    if contra_account is None or contra_account.company_id != transaction.company_id:
        raise BankImportError("Gegenkonto nicht gefunden.")

    gross = abs(transaction.amount)
    contra_net = gross
    if tax_code_id is not None:
        tax_code = session.get(TaxCode, tax_code_id)
        if tax_code is None or tax_code.company_id != transaction.company_id:
            raise BankImportError("Steuercode nicht gefunden.")
        contra_net, _ = net_from_gross(gross, tax_code.rate)

    zero = Decimal("0.00")
    incoming = transaction.amount > 0
    bank_line = JournalLineInput(
        account_id=transaction.bank_account_id,
        debit_amount=gross if incoming else zero,
        credit_amount=zero if incoming else gross,
    )
    contra_line = JournalLineInput(
        account_id=contra_account.id,
        debit_amount=zero if incoming else contra_net,
        credit_amount=contra_net if incoming else zero,
        tax_code_id=tax_code_id,
    )

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=transaction.company_id,
            entry_date=transaction.booking_date,
            description=description or f"Bank: {transaction.purpose}",
            status="posted",
            changed_by=changed_by,
            lines=[bank_line, contra_line],
        ),
    )

    transaction.journal_entry_id = entry.id
    transaction.status = "booked"

    log_audit_event(
        session=session,
        tenant_id=transaction.tenant_id,
        company_id=transaction.company_id,
        entity_type="bank_transaction",
        entity_id=str(transaction.id),
        action="booked",
        changed_by=changed_by,
        payload={"journal_entry_id": entry.id, "posting_number": entry.posting_number},
    )
    session.commit()
    session.refresh(transaction)
    return transaction


def _sniff_delimiter(csv_stream: TextIO) -> str:
    sample = csv_stream.read(2048)
    csv_stream.seek(0)
    if sample.count(";") > sample.count(","):
        return ";"
    return ","


def _resolve_header_mapping(field_names: list[str]) -> dict[str, str]:
    normalized_headers = {name.strip().lower(): name for name in field_names}
    mapping: dict[str, str] = {}
    for canonical, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized_headers:
                mapping[canonical] = normalized_headers[alias]
                break

    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        raise BankImportError(f"CSV-Kopfzeile: Pflichtspalten fehlen: {', '.join(missing)}")
    return mapping


def _parse_date(value: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise BankImportError(f"Ungültiges Datum: {value}")


def _parse_amount(value: str) -> Decimal:
    normalized = value.replace(" ", "").replace(" ", "").replace("€", "")
    if "," in normalized:
        # Deutsches Format: 1.234,56
        normalized = normalized.replace(".", "").replace(",", ".")
    try:
        return Decimal(normalized).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise BankImportError(f"Ungültiger Betrag: {value}") from exc


def _dedup_hash(
    *,
    bank_account_id: int,
    booking_date: date,
    amount: Decimal,
    purpose: str,
    counterparty: str | None,
) -> str:
    raw = f"{bank_account_id}|{booking_date.isoformat()}|{amount}|{purpose}|{counterparty or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_error(report: BankImportReport, line_number: int, message: str) -> None:
    report.error_rows += 1
    report.errors.append(BankImportRowError(line_number=line_number, message=message))
    logger.warning("Bank import error in line %s: %s", line_number, message)
