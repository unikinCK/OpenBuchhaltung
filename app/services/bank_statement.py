"""Parser für Kontoauszugs-Formate: CAMT.053 (ISO 20022) und MT940 (SWIFT).

Beide Formate werden in ein gemeinsames Zeilenmodell (`BankStatementRow`)
überführt, das die Import-Pipeline in `bank_import` weiterverarbeitet.
Der CAMT-Parser arbeitet wie der E-Rechnungs-Import namespace-agnostisch
über lokale Elementnamen, damit camt.053.001.02 bis .08 gleichermaßen
funktionieren. MT940 wird über die mt-940-Bibliothek geparst, die auch
die deutschen ?-Subfelder (SVWZ, Auftraggeber) auflöst — dieselbe
Bibliothek, die python-fints für den Direktabruf nutzt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterator
from xml.etree import ElementTree as ET

import mt940

MAX_TEXT_LENGTH = 255
MAX_REFERENCE_LENGTH = 64

# Übliche Platzhalter der Banken für "keine Referenz vorhanden".
_REFERENCE_PLACEHOLDERS = {"NONREF", "NOTPROVIDED"}


class BankStatementParseError(ValueError):
    """Raised when a bank statement file cannot be parsed."""


@dataclass(slots=True)
class BankStatementRow:
    """Eine importierbare Umsatzzeile; position dient der Fehlerreferenz."""

    position: int
    booking_date: date
    amount: Decimal
    purpose: str
    counterparty: str | None = None
    currency_code: str | None = None
    bank_reference: str | None = None


@dataclass(slots=True)
class BankStatementRowError:
    position: int
    message: str


StatementItem = BankStatementRow | BankStatementRowError


def detect_statement_format(file_name: str, content: bytes) -> str:
    """Bestimmt das Format ("csv", "camt" oder "mt940") aus Endung und Inhalt."""
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if suffix == "csv":
        return "csv"
    if suffix == "xml":
        return "camt"
    if suffix in {"sta", "mt940", "940"}:
        return "mt940"

    head = content.lstrip(b"\xef\xbb\xbf \t\r\n")[:4096]
    if head.startswith(b"<"):
        return "camt"
    if b":61:" in head and b":20:" in head:
        return "mt940"
    return "csv"


def decode_statement_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Deutsche Banken liefern CSV/MT940 häufig als ISO-8859-1.
        return content.decode("latin-1")


def _clip(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value[:MAX_TEXT_LENGTH] or None


def _clean_reference(value: object) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    if not text or text.upper() in _REFERENCE_PLACEHOLDERS:
        return None
    return text[:MAX_REFERENCE_LENGTH]


# ---------------------------------------------------------------------------
# CAMT.053


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == local_name]


def _first(element: ET.Element | None, *path: str) -> ET.Element | None:
    current = element
    for name in path:
        if current is None:
            return None
        matches = _children(current, name)
        current = matches[0] if matches else None
    return current


def _text(element: ET.Element | None, *path: str) -> str | None:
    target = _first(element, *path) if path else element
    if target is None or target.text is None:
        return None
    return target.text.strip() or None


def _iter_descendants(element: ET.Element, local_name: str) -> Iterator[ET.Element]:
    for child in element.iter():
        if _local(child.tag) == local_name:
            yield child


def _parse_camt_date(entry: ET.Element) -> date | None:
    for date_tag in ("BookgDt", "ValDt"):
        for value_tag in ("Dt", "DtTm"):
            raw = _text(entry, date_tag, value_tag)
            if raw:
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    continue
    return None


def _camt_purpose(entry: ET.Element) -> str | None:
    unstructured = [
        text.strip()
        for ustrd in _iter_descendants(entry, "Ustrd")
        if (text := ustrd.text or "").strip()
    ]
    if unstructured:
        return " ".join(unstructured)
    return _text(entry, "AddtlNtryInf")


def _camt_reference(entry: ET.Element) -> str | None:
    """Bankseitige Referenz eines Ntry: AcctSvcrRef, sonst EndToEndId/TxId aus Refs."""
    reference = _clean_reference(_text(entry, "AcctSvcrRef"))
    if reference:
        return reference
    for refs in _iter_descendants(entry, "Refs"):
        for tag in ("EndToEndId", "TxId", "AcctSvcrRef"):
            reference = _clean_reference(_text(refs, tag))
            if reference:
                return reference
    return None


def _camt_counterparty(entry: ET.Element, incoming: bool) -> str | None:
    party_tag = "Dbtr" if incoming else "Cdtr"
    for parties in _iter_descendants(entry, "RltdPties"):
        for party in _children(parties, party_tag):
            for name in _iter_descendants(party, "Nm"):
                if name.text and name.text.strip():
                    return name.text.strip()
    return None


def parse_camt053(content: bytes) -> list[StatementItem]:
    """Liest alle Ntry-Elemente eines CAMT.053/052-Dokuments.

    Sammelbuchungen (ein Ntry mit mehreren TxDtls) werden bewusst als eine
    Zeile mit dem Summenbetrag des Ntry importiert; die Verwendungszwecke
    aller Einzeltransaktionen werden aneinandergereiht.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise BankStatementParseError(f"CAMT-XML kann nicht gelesen werden: {exc}") from exc

    entries = list(_iter_descendants(root, "Ntry"))
    if not entries:
        raise BankStatementParseError(
            "CAMT-Datei enthält keine Umsätze (kein Ntry-Element gefunden)."
        )

    items: list[StatementItem] = []
    for position, entry in enumerate(entries, start=1):
        credit_debit = (_text(entry, "CdtDbtInd") or "").upper()
        if credit_debit not in {"CRDT", "DBIT"}:
            items.append(
                BankStatementRowError(position, "CdtDbtInd fehlt oder ist ungültig.")
            )
            continue

        raw_amount = _text(entry, "Amt")
        try:
            amount = Decimal(raw_amount).quantize(Decimal("0.01")) if raw_amount else None
        except InvalidOperation:
            amount = None
        if amount is None:
            items.append(BankStatementRowError(position, f"Ungültiger Betrag: {raw_amount}"))
            continue

        incoming = credit_debit == "CRDT"
        if not incoming:
            amount = -amount
        if (_text(entry, "RvslInd") or "").lower() in {"true", "1"}:
            amount = -amount

        booking_date = _parse_camt_date(entry)
        if booking_date is None:
            items.append(BankStatementRowError(position, "Buchungsdatum (BookgDt/ValDt) fehlt."))
            continue

        amount_element = _first(entry, "Amt")
        currency = amount_element.get("Ccy") if amount_element is not None else None
        purpose = _clip(_camt_purpose(entry)) or "(kein Verwendungszweck)"
        counterparty = _clip(_camt_counterparty(entry, incoming=amount > 0))

        items.append(
            BankStatementRow(
                position=position,
                booking_date=booking_date,
                amount=amount,
                purpose=purpose,
                counterparty=counterparty,
                currency_code=currency,
                bank_reference=_camt_reference(entry),
            )
        )
    return items


# ---------------------------------------------------------------------------
# MT940


def row_from_mt940_data(position: int, data: dict) -> StatementItem:
    """Übersetzt eine geparste MT940-Transaktion (mt-940-Bibliothek) in eine Zeile.

    Wird sowohl für den Dateiimport als auch für per FinTS abgerufene
    Umsätze verwendet (python-fints liefert dieselben mt940-Objekte).
    """
    amount_obj = data.get("amount")
    if amount_obj is None or amount_obj.amount is None:
        return BankStatementRowError(position, "Betrag fehlt im :61:-Feld.")
    amount = Decimal(amount_obj.amount).quantize(Decimal("0.01"))

    booking_date = data.get("entry_date") or data.get("guessed_entry_date") or data.get("date")
    if isinstance(booking_date, datetime):
        booking_date = booking_date.date()
    if not isinstance(booking_date, date):
        return BankStatementRowError(position, "Buchungsdatum fehlt im :61:-Feld.")

    purpose = data.get("purpose")
    if not purpose:
        details = data.get("transaction_details") or ""
        purpose = " ".join(details.split()) or data.get("posting_text")
    purpose = _clip(purpose) or "(kein Verwendungszweck)"

    counterparty = _clip(data.get("applicant_name"))
    currency = data.get("currency") or getattr(amount_obj, "currency", None)
    bank_reference = (
        _clean_reference(data.get("bank_reference"))
        or _clean_reference(data.get("end_to_end_reference"))
        or _clean_reference(data.get("customer_reference"))
    )

    return BankStatementRow(
        position=position,
        booking_date=booking_date,
        amount=amount,
        purpose=purpose,
        counterparty=counterparty,
        currency_code=currency,
        bank_reference=bank_reference,
    )


def parse_mt940(content: bytes) -> list[StatementItem]:
    """Liest die Umsatzzeilen (:61:/:86:) einer MT940-Datei."""
    text = decode_statement_text(content).replace("@@", "\r\n")
    try:
        transactions = mt940.models.Transactions()
        parsed = transactions.parse(text)
    except Exception as exc:  # mt-940 wirft u. a. RuntimeError und ValueError
        raise BankStatementParseError(f"MT940-Datei kann nicht gelesen werden: {exc}") from exc

    if not parsed:
        raise BankStatementParseError("MT940-Datei enthält keine Umsätze.")

    return [
        row_from_mt940_data(position, transaction.data)
        for position, transaction in enumerate(parsed, start=1)
    ]
