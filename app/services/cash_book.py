"""Kassenbuch (ADR-007): fortlaufend nummeriert, verkettet und unveränderbar.

Jeder Eintrag erzeugt in derselben Transaktion genau eine Buchung; Korrekturen laufen nur
über Storno-Einträge. Das Kassenbuch wird von Hand geführt. Es ersetzt weder eine TSE noch
den DSFinV-K-Export elektronischer Kassensysteme und ist nicht zertifiziert.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import GENESIS_HASH, _canonical_timestamp, log_audit_event
from app.services.bank_import import net_from_gross
from app.services.journal_entries import (
    SOURCE_CARRYFORWARD,
    SOURCE_CASH_BOOK,
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
    resolve_partner_reference,
    reverse_journal_entry,
)
from domain.formatting import format_amount
from domain.models import (
    CASH_DIRECTION_IN,
    CASH_DIRECTION_OUT,
    CASH_ENTRY_DIFFERENCE,
    CASH_ENTRY_OPENING,
    CASH_ENTRY_PAYMENT,
    CASH_ENTRY_RECEIPT,
    CASH_ENTRY_REVERSAL,
    Account,
    BankTransaction,
    CashBook,
    CashClosing,
    CashEntry,
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError

CASH_ENTRY_HASH_VERSION = 1
ZERO = Decimal("0.00")
CENT = Decimal("0.01")
# Grenze der Spalten mit Numeric(14, 2).
MAX_AMOUNT = Decimal("1000000000000.00")
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 255
# Buchungen am Kassenbuch vorbei: so viele nennt die Prüfung einzeln.
UNLINKED_POSTINGS_LIMIT = 20

ENTRY_TYPE_LABELS = {
    CASH_ENTRY_RECEIPT: "Einnahme",
    CASH_ENTRY_PAYMENT: "Ausgabe",
    CASH_ENTRY_OPENING: "Anfangsbestand",
    CASH_ENTRY_DIFFERENCE: "Kassendifferenz",
    CASH_ENTRY_REVERSAL: "Storno",
}
_DIRECTION_BY_USER_TYPE = {
    CASH_ENTRY_RECEIPT: CASH_DIRECTION_IN,
    CASH_ENTRY_PAYMENT: CASH_DIRECTION_OUT,
}
_FIXED_DIRECTIONS = {**_DIRECTION_BY_USER_TYPE, CASH_ENTRY_OPENING: CASH_DIRECTION_IN}

# Der Server läuft oft in UTC; der Kassentag folgt der deutschen Ortszeit.
BUSINESS_TIMEZONE = ZoneInfo("Europe/Berlin")

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


class CashBookError(ValueError):
    """Ein Kassenbuch-Vorgang ist fachlich nicht möglich."""


def business_today() -> date:
    return datetime.now(BUSINESS_TIMEZONE).date()


def _signed(entry: CashEntry) -> Decimal:
    return entry.amount if entry.direction == CASH_DIRECTION_IN else -entry.amount


def _euro(value: Decimal) -> str:
    return f"{format_amount(value)} €"


def _german_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _to_cents(value: object) -> Decimal:
    return Decimal(str(value)).quantize(CENT)


@contextmanager
def _transaction(session: Session) -> Iterator[None]:
    """Eintrag, Buchung, Nummernkreis und Audit gemeinsam oder gar nicht."""
    try:
        yield
        session.commit()
    except CashBookError:
        session.rollback()
        raise
    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        session.rollback()
        raise CashBookError(str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        message = str(exc.orig if exc.orig is not None else exc)
        if "cash_" in message:
            raise CashBookError(
                "Das Kassenbuch wurde gerade von jemand anderem geändert oder der Eintrag "
                "ist bereits vorhanden. Bitte die Seite neu laden und erneut versuchen."
            ) from exc
        raise


def _lock_book(session: Session, cash_book_id: int) -> CashBook:
    """Sperrt die Kasse: Nummer, Vorgänger-Hash und Bestand sind damit serialisiert."""
    book = session.execute(
        select(CashBook)
        .where(CashBook.id == cash_book_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if book is None:
        raise CashBookError("Kasse nicht gefunden.")
    return book


def _clean_name(name: str | None) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise CashBookError("Bitte einen Namen für die Kasse angeben.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise CashBookError(f"Der Name darf höchstens {MAX_NAME_LENGTH} Zeichen lang sein.")
    return cleaned


def _clean_description(description: str | None) -> str:
    cleaned = (description or "").strip()
    if not cleaned:
        raise CashBookError("Bitte eine Beschreibung angeben.")
    if len(cleaned) > MAX_DESCRIPTION_LENGTH:
        raise CashBookError(
            f"Die Beschreibung darf höchstens {MAX_DESCRIPTION_LENGTH} Zeichen lang sein."
        )
    return cleaned


def _ensure_name_is_free(
    session: Session, *, company_id: int, name: str, except_id: int | None = None
) -> None:
    stmt = select(CashBook.id).where(CashBook.company_id == company_id, CashBook.name == name)
    if except_id is not None:
        stmt = stmt.where(CashBook.id != except_id)
    if session.execute(stmt).first() is not None:
        raise CashBookError(f"Es gibt bereits eine Kasse mit dem Namen {name}.")


def last_entry(session: Session, cash_book_id: int) -> CashEntry | None:
    return session.execute(
        select(CashEntry)
        .where(CashEntry.cash_book_id == cash_book_id)
        .order_by(CashEntry.entry_number.desc())
        .limit(1)
    ).scalar_one_or_none()


def current_balance(session: Session, cash_book_id: int) -> Decimal:
    entry = last_entry(session, cash_book_id)
    return entry.balance_after if entry is not None else ZERO


def last_closing(session: Session, cash_book_id: int) -> CashClosing | None:
    return session.execute(
        select(CashClosing)
        .where(CashClosing.cash_book_id == cash_book_id)
        .order_by(CashClosing.closing_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def account_balance(session: Session, account_id: int, *, as_of: date | None = None) -> Decimal:
    """Kontensaldo (Soll − Haben); Saldovorträge wiederholen nur die Vorjahre."""
    stmt = (
        select(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(
            JournalEntryLine.account_id == account_id,
            JournalEntry.source != SOURCE_CARRYFORWARD,
        )
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    debit, credit = session.execute(stmt).one()
    return _to_cents(debit) - _to_cents(credit)


def calculate_cash_entry_hash(entry: CashEntry) -> str:
    """SHA-256 über die kanonischen Felder einschließlich des Vorgänger-Hashs."""
    if entry.hash_version != CASH_ENTRY_HASH_VERSION:
        raise ValueError(f"Unsupported cash entry hash version: {entry.hash_version}")
    canonical = {
        "amount": f"{entry.amount:.2f}",
        "balance_after": f"{entry.balance_after:.2f}",
        "cash_book_id": entry.cash_book_id,
        "company_id": entry.company_id,
        "contra_account_id": entry.contra_account_id,
        "created_at": _canonical_timestamp(entry.created_at),
        "created_by": entry.created_by,
        "description": entry.description,
        "direction": entry.direction,
        "document_id": entry.document_id,
        "entry_date": entry.entry_date.isoformat(),
        "entry_number": entry.entry_number,
        "entry_type": entry.entry_type,
        "hash_version": entry.hash_version,
        "journal_entry_id": entry.journal_entry_id,
        "partner_id": entry.partner_id,
        "previous_hash": entry.previous_hash,
        "reversal_of_id": entry.reversal_of_id,
        "tax_code_id": entry.tax_code_id,
        "tenant_id": entry.tenant_id,
    }
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contra_account(session: Session, book: CashBook, account_id: int | None) -> Account:
    if not account_id:
        raise CashBookError("Bitte ein Gegenkonto wählen.")
    account = session.get(Account, account_id)
    if account is None or account.company_id != book.company_id:
        raise CashBookError("Gegenkonto nicht gefunden.")
    if account.id == book.account_id:
        raise CashBookError("Das Gegenkonto darf nicht das Kassenkonto selbst sein.")
    return account


def _tax_code(session: Session, book: CashBook, tax_code_id: int | None) -> TaxCode | None:
    if tax_code_id is None:
        return None
    tax_code = session.get(TaxCode, tax_code_id)
    if tax_code is None or tax_code.company_id != book.company_id:
        raise CashBookError("Steuercode nicht gefunden.")
    if not tax_code.is_active:
        raise CashBookError("Inaktive Steuercodes dürfen nicht verwendet werden.")
    return tax_code


def _document(session: Session, book: CashBook, document_id: int | None) -> Document | None:
    if document_id is None:
        return None
    document = session.get(Document, document_id)
    if document is None or document.company_id != book.company_id:
        raise CashBookError("Beleg nicht gefunden.")
    if not document.is_current:
        raise CashBookError("Der Beleg ist nicht die aktuelle Fassung.")
    if document.journal_entry_id is not None:
        raise CashBookError("Der Beleg ist bereits mit einer Buchung verknüpft.")
    return document


def _booking_lines(
    *,
    cash_account_id: int,
    contra_account_id: int,
    gross: Decimal,
    incoming: bool,
    tax_code: TaxCode | None,
) -> list[JournalLineInput]:
    """Kasse an Gegenkonto bzw. umgekehrt; Steuer wie bei der Bankbuchung aus dem Brutto."""
    net, tax_amount = net_from_gross(gross, tax_code.rate) if tax_code else (gross, ZERO)
    if tax_code is not None and tax_amount > ZERO and tax_code.vat_account_id is None:
        raise CashBookError(f"Steuercode {tax_code.code} hat kein Steuerkonto hinterlegt.")
    lines = [
        JournalLineInput(
            account_id=cash_account_id,
            debit_amount=gross if incoming else ZERO,
            credit_amount=ZERO if incoming else gross,
        ),
        JournalLineInput(
            account_id=contra_account_id,
            debit_amount=ZERO if incoming else net,
            credit_amount=net if incoming else ZERO,
            tax_code_id=tax_code.id if tax_code else None,
        ),
    ]
    if tax_code is not None and tax_amount > ZERO:
        lines.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                debit_amount=ZERO if incoming else tax_amount,
                credit_amount=tax_amount if incoming else ZERO,
                description=(
                    f"{tax_code.code} {tax_code.rate}% auf {net} (aus Brutto {gross})"
                ),
                tax_code_id=tax_code.id,
            )
        )
    return lines


def _ensure_writable(book: CashBook) -> None:
    if not book.is_active:
        raise CashBookError(
            f"Die Kasse {book.name} ist inaktiv und nimmt keine neuen Einträge an."
        )


def _ensure_entry_date_allowed(session: Session, book: CashBook, entry_date: date) -> None:
    if entry_date > business_today():
        raise CashBookError(
            f"Das Datum {_german_date(entry_date)} liegt in der Zukunft. Das Kassenbuch "
            "nimmt nur bereits geschehene Vorgänge auf."
        )
    previous = last_entry(session, book.id)
    if previous is not None and entry_date < previous.entry_date:
        raise CashBookError(
            f"Das Kassenbuch ist chronologisch: Der letzte Eintrag (Nr. "
            f"{previous.entry_number}) stammt vom {_german_date(previous.entry_date)}, "
            "ein neuer Eintrag darf nicht davor liegen."
        )
    closing = last_closing(session, book.id)
    if closing is not None and entry_date <= closing.closing_date:
        raise CashBookError(
            f"Der Tag {_german_date(entry_date)} ist durch den Kassensturz vom "
            f"{_german_date(closing.closing_date)} abgeschlossen."
        )


def _append_entry(
    *,
    session: Session,
    book: CashBook,
    entry_type: str,
    direction: str,
    entry_date: date,
    amount: Decimal,
    description: str,
    changed_by: str,
    contra_account: Account | None = None,
    tax_code: TaxCode | None = None,
    partner_id: int | None = None,
    document: Document | None = None,
    document_id: int | None = None,
    journal_entry: JournalEntry | None = None,
    reversal_of: CashEntry | None = None,
) -> CashEntry:
    """Hängt einen Eintrag an die Kette. Bucht, wenn ein Gegenkonto und keine Buchung
    übergeben ist; committet nicht."""
    _ensure_writable(book)
    _ensure_entry_date_allowed(session, book, entry_date)
    if amount <= ZERO:
        raise CashBookError("Der Betrag muss größer als null sein.")
    if amount >= MAX_AMOUNT:
        raise CashBookError("Der Betrag ist zu groß.")

    previous = last_entry(session, book.id)
    previous_balance = previous.balance_after if previous is not None else ZERO
    incoming = direction == CASH_DIRECTION_IN
    balance_after = previous_balance + amount if incoming else previous_balance - amount
    if balance_after < ZERO:
        raise CashBookError(
            f"Der Kassenbestand von {_euro(previous_balance)} reicht nicht für "
            f"{_euro(amount)}; er würde negativ. Ein Kassenfehlbetrag ist bei einer "
            "Betriebsprüfung ein Warnsignal, weil er auf nicht erfasste Einnahmen deuten "
            "kann. Bitte prüfen, ob eine Einnahme oder Einlage fehlt oder das Datum stimmt."
        )

    entry_number = book.last_entry_number + 1
    if journal_entry is None and contra_account is not None:
        journal_entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=book.company_id,
                entry_date=entry_date,
                description=f"Kasse {book.name} Nr. {entry_number}: {description}",
                status="posted",
                changed_by=changed_by,
                lines=_booking_lines(
                    cash_account_id=book.account_id,
                    contra_account_id=contra_account.id,
                    gross=amount,
                    incoming=incoming,
                    tax_code=tax_code,
                ),
                source=SOURCE_CASH_BOOK,
                expand_tax_lines=False,
                partner_id=partner_id,
            ),
            commit=False,
        )

    book.last_entry_number = entry_number
    entry = CashEntry(
        tenant_id=book.tenant_id,
        company_id=book.company_id,
        cash_book_id=book.id,
        entry_number=entry_number,
        entry_date=entry_date,
        entry_type=entry_type,
        direction=direction,
        amount=amount,
        description=description,
        contra_account_id=contra_account.id if contra_account is not None else None,
        tax_code_id=tax_code.id if tax_code is not None else None,
        partner_id=partner_id,
        document_id=document.id if document is not None else document_id,
        journal_entry_id=journal_entry.id if journal_entry is not None else None,
        balance_after=balance_after,
        reversal_of_id=reversal_of.id if reversal_of is not None else None,
        created_by=changed_by,
        created_at=datetime.now(timezone.utc),
        hash_version=CASH_ENTRY_HASH_VERSION,
        previous_hash=previous.content_hash if previous is not None else GENESIS_HASH,
    )
    entry.content_hash = calculate_cash_entry_hash(entry)
    session.add(entry)
    session.flush()

    if document is not None and journal_entry is not None:
        document.journal_entry_id = journal_entry.id
        log_audit_event(
            session=session,
            tenant_id=document.tenant_id,
            company_id=document.company_id,
            entity_type="document",
            entity_id=str(document.id),
            action="linked",
            changed_by=changed_by,
            payload={"previous_journal_entry_id": None, "journal_entry_id": journal_entry.id},
        )
    log_audit_event(
        session=session,
        tenant_id=book.tenant_id,
        company_id=book.company_id,
        entity_type="cash_entry",
        entity_id=str(entry.id),
        action="created",
        changed_by=changed_by,
        payload={
            "cash_book_id": book.id,
            "entry_number": entry.entry_number,
            "entry_type": entry.entry_type,
            "direction": entry.direction,
            "entry_date": entry.entry_date.isoformat(),
            "amount": f"{entry.amount:.2f}",
            "balance_after": f"{entry.balance_after:.2f}",
            "journal_entry_id": entry.journal_entry_id,
            "reversal_of_id": entry.reversal_of_id,
            "content_hash": entry.content_hash,
        },
    )
    return entry


def _book_snapshot(book: CashBook) -> dict[str, Any]:
    return {
        "name": book.name,
        "account_id": book.account_id,
        "is_active": book.is_active,
    }


def _cash_account(session: Session, *, company_id: int, account_id: int | None) -> Account:
    if not account_id:
        raise CashBookError("Bitte das Kassenkonto wählen.")
    account = session.get(Account, account_id)
    if account is None or account.company_id != company_id:
        raise CashBookError("Konto nicht gefunden.")
    if account.account_type != "asset":
        raise CashBookError(
            f"Konto {account.code} ist kein Aktivkonto und kann keine Kasse tragen."
        )
    if account.subledger is not None:
        raise CashBookError(f"Konto {account.code} ist ein Sammelkonto, kein Kassenkonto.")
    if not account.is_active:
        raise CashBookError(f"Konto {account.code} ist inaktiv.")
    has_bank_transactions = session.execute(
        select(BankTransaction.id).where(BankTransaction.bank_account_id == account.id).limit(1)
    ).first()
    if has_bank_transactions is not None:
        raise CashBookError(f"Konto {account.code} ist ein Bankkonto (es hat Bankumsätze).")
    in_use = session.execute(
        select(CashBook.name).where(
            CashBook.company_id == company_id, CashBook.account_id == account.id
        )
    ).scalar()
    if in_use is not None:
        raise CashBookError(f"Konto {account.code} gehört bereits zur Kasse {in_use}.")
    return account


def create_cash_book(
    *,
    session: Session,
    company_id: int,
    account_id: int | None,
    name: str | None,
    changed_by: str,
    opening_amount: object | None = None,
    opening_date: date | None = None,
    opening_contra_account_id: int | None = None,
) -> CashBook:
    """Legt eine Kasse an; der Anfangsbestand wird ihr erster Eintrag (ADR-007).

    Mit Gegenkonto wird er gebucht (Kasse an Gegenkonto); das Konto darf dann noch keinen
    Saldo haben. Ohne Gegenkonto muss das Konto den Bestand am Stichtag bereits tragen,
    es entsteht keine Buchung. Ohne Betrag gilt dann der Kontensaldo.
    """
    with _transaction(session):
        company = session.get(Company, company_id)
        if company is None:
            raise CashBookError("Gesellschaft nicht gefunden.")
        name = _clean_name(name)
        _ensure_name_is_free(session, company_id=company_id, name=name)
        account = _cash_account(session, company_id=company_id, account_id=account_id)
        opening_date = opening_date or business_today()
        amount = None if opening_amount in (None, "") else parse_decimal(str(opening_amount))
        if amount is not None and amount < ZERO:
            raise CashBookError("Der Anfangsbestand darf nicht negativ sein.")

        carried = account_balance(session, account.id, as_of=opening_date)
        if opening_contra_account_id:
            if amount is None or amount == ZERO:
                raise CashBookError(
                    "Ein Gegenkonto ist nur für einen Anfangsbestand über null nötig."
                )
            if carried != ZERO:
                raise CashBookError(
                    f"Konto {account.code} trägt am {_german_date(opening_date)} bereits "
                    f"{_euro(carried)}. Den Anfangsbestand bitte ohne Gegenkonto übernehmen, "
                    "sonst wäre der Bestand doppelt gebucht."
                )
        else:
            if carried < ZERO:
                raise CashBookError(
                    f"Konto {account.code} hat am {_german_date(opening_date)} einen negativen "
                    f"Saldo von {_euro(carried)}. Bitte das Konto zuerst korrigieren."
                )
            if amount is None:
                amount = carried
            if amount != carried:
                raise CashBookError(
                    f"Konto {account.code} trägt am {_german_date(opening_date)} "
                    f"{_euro(carried)}, nicht {_euro(amount)}. Ohne Gegenkonto muss der "
                    "Anfangsbestand dem Kontensaldo entsprechen; eine gezählte Abweichung "
                    "gehört anschließend in den Kassensturz."
                )

        book = CashBook(
            tenant_id=company.tenant_id,
            company_id=company.id,
            account_id=account.id,
            name=name,
            is_active=True,
            last_entry_number=0,
            created_by=changed_by,
        )
        session.add(book)
        session.flush()
        if amount > ZERO:
            contra = (
                _contra_account(session, book, opening_contra_account_id)
                if opening_contra_account_id
                else None
            )
            _append_entry(
                session=session,
                book=book,
                entry_type=CASH_ENTRY_OPENING,
                direction=CASH_DIRECTION_IN,
                entry_date=opening_date,
                amount=amount,
                description="Anfangsbestand",
                changed_by=changed_by,
                contra_account=contra,
            )
        log_audit_event(
            session=session,
            tenant_id=book.tenant_id,
            company_id=book.company_id,
            entity_type="cash_book",
            entity_id=str(book.id),
            action="created",
            changed_by=changed_by,
            payload={"before": None, "after": _book_snapshot(book)},
        )
    session.refresh(book)
    return book


def update_cash_book(
    *,
    session: Session,
    cash_book_id: int,
    changed_by: str,
    name: str | None = None,
    is_active: bool | None = None,
) -> CashBook:
    """Ändert Name und Aktivstatus; das Konto bleibt fest."""
    with _transaction(session):
        book = _lock_book(session, cash_book_id)
        before = _book_snapshot(book)
        if name is not None:
            cleaned = _clean_name(name)
            _ensure_name_is_free(
                session, company_id=book.company_id, name=cleaned, except_id=book.id
            )
            book.name = cleaned
        if is_active is not None:
            balance = current_balance(session, book.id)
            if not is_active and book.is_active and balance != ZERO:
                raise CashBookError(
                    f"Die Kasse hat noch einen Bestand von {_euro(balance)}. Bitte den "
                    "Bestand zuerst ausbuchen (z. B. als Ausgabe gegen Geldtransit)."
                )
            book.is_active = is_active
        after = _book_snapshot(book)
        if before != after:
            log_audit_event(
                session=session,
                tenant_id=book.tenant_id,
                company_id=book.company_id,
                entity_type="cash_book",
                entity_id=str(book.id),
                action="updated",
                changed_by=changed_by,
                payload={"before": before, "after": after},
            )
    session.refresh(book)
    return book


def create_cash_entry(
    *,
    session: Session,
    cash_book_id: int,
    entry_type: str | None,
    entry_date: date | None,
    amount: object,
    description: str | None,
    contra_account_id: int | None,
    changed_by: str,
    tax_code_id: int | None = None,
    partner_id: int | None = None,
    partner_number: str | None = None,
    document_id: int | None = None,
) -> CashEntry:
    """Erfasst eine Einnahme oder Ausgabe (Bruttobetrag) samt Buchung in einer Transaktion."""
    with _transaction(session):
        book = _lock_book(session, cash_book_id)
        direction = _DIRECTION_BY_USER_TYPE.get(entry_type or "")
        if direction is None:
            raise CashBookError("Die Art muss receipt (Einnahme) oder payment (Ausgabe) sein.")
        if entry_date is None:
            raise CashBookError("Bitte ein Datum angeben.")
        entry = _append_entry(
            session=session,
            book=book,
            entry_type=entry_type,
            direction=direction,
            entry_date=entry_date,
            amount=parse_decimal(str(amount)),
            description=_clean_description(description),
            changed_by=changed_by,
            contra_account=_contra_account(session, book, contra_account_id),
            tax_code=_tax_code(session, book, tax_code_id),
            partner_id=resolve_partner_reference(
                session=session,
                company_id=book.company_id,
                partner_id=partner_id,
                partner_number=partner_number,
            ),
            document=_document(session, book, document_id),
        )
    session.refresh(entry)
    return entry


def _release_document(*, session: Session, original: CashEntry, changed_by: str) -> None:
    """Gibt den Beleg eines stornierten Eintrags frei, damit die Neuerfassung ihn nutzen kann.

    Eine Buchung hält den Beleg nur einzeln; der Eintrag und sein Storno behalten den Verweis.
    """
    document = original.document
    if document is None or document.journal_entry_id != original.journal_entry_id:
        return
    document.journal_entry_id = None
    log_audit_event(
        session=session,
        tenant_id=document.tenant_id,
        company_id=document.company_id,
        entity_type="document",
        entity_id=str(document.id),
        action="linked",
        changed_by=changed_by,
        payload={
            "previous_journal_entry_id": original.journal_entry_id,
            "journal_entry_id": None,
        },
    )


def reverse_cash_entry(
    *, session: Session, cash_book_id: int, entry_id: int, changed_by: str
) -> CashEntry:
    """Storniert einen Eintrag: Storno-Eintrag am aktuellen Datum samt Stornobuchung."""
    with _transaction(session):
        book = _lock_book(session, cash_book_id)
        original = session.get(CashEntry, entry_id)
        if original is None or original.cash_book_id != book.id:
            raise CashBookError("Eintrag nicht gefunden.")
        if original.entry_type == CASH_ENTRY_REVERSAL:
            raise CashBookError(
                f"Eintrag Nr. {original.entry_number} ist selbst ein Storno und kann nicht "
                "storniert werden."
            )
        reversed_by = session.execute(
            select(CashEntry.entry_number).where(CashEntry.reversal_of_id == original.id)
        ).scalar()
        if reversed_by is not None:
            raise CashBookError(
                f"Eintrag Nr. {original.entry_number} wurde bereits storniert "
                f"(Eintrag Nr. {reversed_by})."
            )
        if original.journal_entry_id is None:
            raise CashBookError(
                "Ein Anfangsbestand ohne Buchung entspricht dem Kontensaldo und lässt sich "
                "nicht stornieren. Eine Abweichung gehört in den Kassensturz."
            )
        today = business_today()
        journal_entry = reverse_journal_entry(
            session=session,
            journal_entry_id=original.journal_entry_id,
            reversal_date=today,
            changed_by=changed_by,
            commit=False,
            from_cash_book=True,
        )
        description = f"Storno Nr. {original.entry_number}: {original.description}"
        entry = _append_entry(
            session=session,
            book=book,
            entry_type=CASH_ENTRY_REVERSAL,
            direction=(
                CASH_DIRECTION_OUT
                if original.direction == CASH_DIRECTION_IN
                else CASH_DIRECTION_IN
            ),
            entry_date=today,
            amount=original.amount,
            description=description[:MAX_DESCRIPTION_LENGTH],
            changed_by=changed_by,
            contra_account=original.contra_account,
            tax_code=original.tax_code,
            partner_id=original.partner_id,
            document_id=original.document_id,
            journal_entry=journal_entry,
            reversal_of=original,
        )
        _release_document(session=session, original=original, changed_by=changed_by)
    session.refresh(entry)
    return entry


def create_cash_closing(
    *,
    session: Session,
    cash_book_id: int,
    closing_date: date | None,
    counted_balance: object,
    changed_by: str,
    difference_account_id: int | None = None,
    description: str | None = None,
) -> CashClosing:
    """Kassensturz: hält Soll und gezählten Bestand fest und sperrt die Tage bis zum Datum.

    Eine Differenz wird als Eintrag der Art ``difference`` gegen das gewählte Konto gebucht.
    """
    with _transaction(session):
        book = _lock_book(session, cash_book_id)
        _ensure_writable(book)
        if closing_date is None:
            raise CashBookError("Bitte ein Datum angeben.")
        if closing_date > business_today():
            raise CashBookError("Ein Kassensturz kann nicht in der Zukunft liegen.")
        previous_closing = last_closing(session, book.id)
        if previous_closing is not None and closing_date <= previous_closing.closing_date:
            raise CashBookError(
                f"Der Tag {_german_date(closing_date)} ist bereits durch den Kassensturz vom "
                f"{_german_date(previous_closing.closing_date)} abgeschlossen."
            )
        previous = last_entry(session, book.id)
        if previous is not None and closing_date < previous.entry_date:
            raise CashBookError(
                f"Es gibt bereits Einträge bis zum {_german_date(previous.entry_date)}. "
                "Der Kassensturz muss am selben Tag oder später liegen."
            )
        counted = parse_decimal(str(counted_balance))
        if counted < ZERO:
            raise CashBookError("Der gezählte Bestand darf nicht negativ sein.")

        expected = previous.balance_after if previous is not None else ZERO
        difference = counted - expected
        difference_entry = None
        if difference != ZERO:
            if not difference_account_id:
                raise CashBookError(
                    f"Soll-Bestand {_euro(expected)}, gezählt {_euro(counted)}: Die Differenz "
                    f"von {_euro(difference)} braucht ein Konto, gegen das sie gebucht wird."
                )
            difference_entry = _append_entry(
                session=session,
                book=book,
                entry_type=CASH_ENTRY_DIFFERENCE,
                direction=CASH_DIRECTION_IN if difference > ZERO else CASH_DIRECTION_OUT,
                entry_date=closing_date,
                amount=abs(difference),
                description=(
                    _clean_description(description)
                    if (description or "").strip()
                    else f"Kassendifferenz Kassensturz {_german_date(closing_date)}"
                ),
                changed_by=changed_by,
                contra_account=_contra_account(session, book, difference_account_id),
            )
        closing = CashClosing(
            tenant_id=book.tenant_id,
            company_id=book.company_id,
            cash_book_id=book.id,
            closing_date=closing_date,
            expected_balance=expected,
            counted_balance=counted,
            difference=difference,
            difference_entry_id=difference_entry.id if difference_entry is not None else None,
            created_by=changed_by,
            created_at=datetime.now(timezone.utc),
        )
        session.add(closing)
        session.flush()
        log_audit_event(
            session=session,
            tenant_id=book.tenant_id,
            company_id=book.company_id,
            entity_type="cash_closing",
            entity_id=str(closing.id),
            action="created",
            changed_by=changed_by,
            payload={
                "cash_book_id": book.id,
                "closing_date": closing_date.isoformat(),
                "expected_balance": f"{expected:.2f}",
                "counted_balance": f"{counted:.2f}",
                "difference": f"{difference:.2f}",
                "difference_entry_id": closing.difference_entry_id,
            },
        )
    session.refresh(closing)
    return closing


def _entry_loaders() -> tuple:
    return (
        selectinload(CashEntry.contra_account),
        selectinload(CashEntry.tax_code),
        selectinload(CashEntry.partner),
        selectinload(CashEntry.document),
        selectinload(CashEntry.journal_entry),
        selectinload(CashEntry.reversal_of),
    )


def reversal_numbers(session: Session, entry_ids: Iterable[int]) -> dict[int, int]:
    """Stornierter Eintrag → Nummer des Storno-Eintrags."""
    ids = list(entry_ids)
    if not ids:
        return {}
    return dict(
        session.execute(
            select(CashEntry.reversal_of_id, CashEntry.entry_number).where(
                CashEntry.reversal_of_id.in_(ids)
            )
        ).all()
    )


def list_cash_books(
    *, session: Session, company_id: int, include_inactive: bool = False
) -> list[CashBook]:
    stmt = (
        select(CashBook)
        .where(CashBook.company_id == company_id)
        .options(selectinload(CashBook.account))
        .order_by(CashBook.name)
    )
    if not include_inactive:
        stmt = stmt.where(CashBook.is_active.is_(True))
    return list(session.execute(stmt).scalars().all())


def list_cash_entries(
    *,
    session: Session,
    cash_book_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[CashEntry], int]:
    """Einträge der Kasse, neueste zuerst, und ihre Gesamtzahl im Filter."""
    filters = [CashEntry.cash_book_id == cash_book_id]
    if date_from is not None:
        filters.append(CashEntry.entry_date >= date_from)
    if date_to is not None:
        filters.append(CashEntry.entry_date <= date_to)
    total = session.execute(select(func.count(CashEntry.id)).where(*filters)).scalar_one()
    entries = (
        session.execute(
            select(CashEntry)
            .where(*filters)
            .options(*_entry_loaders())
            .order_by(CashEntry.entry_number.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(entries), total


def list_cash_closings(
    *,
    session: Session,
    cash_book_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[CashClosing], int]:
    """Abschlüsse der Kasse, neueste zuerst, und ihre Gesamtzahl im Filter."""
    filters = [CashClosing.cash_book_id == cash_book_id]
    if date_from is not None:
        filters.append(CashClosing.closing_date >= date_from)
    if date_to is not None:
        filters.append(CashClosing.closing_date <= date_to)
    total = session.execute(select(func.count(CashClosing.id)).where(*filters)).scalar_one()
    closings = (
        session.execute(
            select(CashClosing)
            .where(*filters)
            .options(selectinload(CashClosing.difference_entry))
            .order_by(CashClosing.closing_date.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return list(closings), total


def serialize_cash_book(session: Session, book: CashBook) -> dict[str, Any]:
    closing = last_closing(session, book.id)
    return {
        "id": book.id,
        "tenant_id": book.tenant_id,
        "company_id": book.company_id,
        "account_id": book.account_id,
        "account_code": book.account.code,
        "account_name": book.account.name,
        "name": book.name,
        "is_active": book.is_active,
        "balance": f"{current_balance(session, book.id):.2f}",
        "entry_count": book.last_entry_number,
        "last_closing_date": closing.closing_date.isoformat() if closing else None,
        "created_by": book.created_by,
        "created_at": book.created_at.isoformat(),
    }


def serialize_cash_entry(
    entry: CashEntry, *, reversed_by_number: int | None = None
) -> dict[str, Any]:
    journal_entry = entry.journal_entry
    return {
        "id": entry.id,
        "cash_book_id": entry.cash_book_id,
        "company_id": entry.company_id,
        "entry_number": entry.entry_number,
        "entry_date": entry.entry_date.isoformat(),
        "entry_type": entry.entry_type,
        "entry_type_label": ENTRY_TYPE_LABELS.get(entry.entry_type, entry.entry_type),
        "direction": entry.direction,
        "amount": f"{entry.amount:.2f}",
        "description": entry.description,
        "contra_account_id": entry.contra_account_id,
        "contra_account_code": entry.contra_account.code if entry.contra_account else None,
        "tax_code_id": entry.tax_code_id,
        "tax_code": entry.tax_code.code if entry.tax_code else None,
        "partner_id": entry.partner_id,
        "partner_name": entry.partner.name if entry.partner else None,
        "document_id": entry.document_id,
        "journal_entry_id": entry.journal_entry_id,
        "posting_number": journal_entry.posting_number if journal_entry else None,
        "balance_after": f"{entry.balance_after:.2f}",
        "reversal_of_id": entry.reversal_of_id,
        "reversal_of_number": entry.reversal_of.entry_number if entry.reversal_of else None,
        "reversed_by_number": reversed_by_number,
        "created_by": entry.created_by,
        "created_at": entry.created_at.isoformat(),
        "previous_hash": entry.previous_hash,
        "content_hash": entry.content_hash,
    }


def serialize_cash_closing(closing: CashClosing) -> dict[str, Any]:
    return {
        "id": closing.id,
        "cash_book_id": closing.cash_book_id,
        "company_id": closing.company_id,
        "closing_date": closing.closing_date.isoformat(),
        "expected_balance": f"{closing.expected_balance:.2f}",
        "counted_balance": f"{closing.counted_balance:.2f}",
        "difference": f"{closing.difference:.2f}",
        "difference_entry_id": closing.difference_entry_id,
        "created_by": closing.created_by,
        "created_at": closing.created_at.isoformat(),
    }


@dataclass(frozen=True, slots=True)
class CashReportDay:
    day: date
    receipts: Decimal
    payments: Decimal
    balance: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "date": self.day.isoformat(),
            "receipts": f"{self.receipts:.2f}",
            "payments": f"{self.payments:.2f}",
            "balance": f"{self.balance:.2f}",
        }


@dataclass(frozen=True, slots=True)
class CashReport:
    """Kassenbericht: Anfangsbestand + Einnahmen − Ausgaben = Endbestand.

    Der Anfangsbestand der Kasse zählt nicht als Einnahme. Storno- und Differenz-Einträge
    zählen nach ihrer Richtung zu Einnahmen bzw. Ausgaben.
    """

    book: CashBook
    date_from: date | None
    date_to: date | None
    opening_balance: Decimal
    total_receipts: Decimal
    total_payments: Decimal
    closing_balance: Decimal
    days: tuple[CashReportDay, ...]
    entries: tuple[CashEntry, ...]
    closings: tuple[CashClosing, ...]
    reversed_by: dict[int, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "cash_book_id": self.book.id,
            "cash_book_name": self.book.name,
            "account_code": self.book.account.code,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "opening_balance": f"{self.opening_balance:.2f}",
            "total_receipts": f"{self.total_receipts:.2f}",
            "total_payments": f"{self.total_payments:.2f}",
            "closing_balance": f"{self.closing_balance:.2f}",
            "days": [day.as_dict() for day in self.days],
            "entries": [
                serialize_cash_entry(
                    entry, reversed_by_number=self.reversed_by.get(entry.id)
                )
                for entry in self.entries
            ],
            "closings": [serialize_cash_closing(closing) for closing in self.closings],
        }


def cash_report(
    *,
    session: Session,
    cash_book_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> CashReport:
    if date_from and date_to and date_from > date_to:
        raise CashBookError("Das Von-Datum darf nicht nach dem Bis-Datum liegen.")
    book = session.get(CashBook, cash_book_id)
    if book is None:
        raise CashBookError("Kasse nicht gefunden.")

    opening = ZERO
    if date_from is not None:
        before = session.execute(
            select(CashEntry.balance_after)
            .where(CashEntry.cash_book_id == book.id, CashEntry.entry_date < date_from)
            .order_by(CashEntry.entry_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        opening = before if before is not None else ZERO

    stmt = (
        select(CashEntry)
        .where(CashEntry.cash_book_id == book.id)
        .options(*_entry_loaders())
        .order_by(CashEntry.entry_number)
    )
    if date_from is not None:
        stmt = stmt.where(CashEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(CashEntry.entry_date <= date_to)
    entries = tuple(session.execute(stmt).scalars().all())

    receipts = payments = ZERO
    per_day: dict[date, list[Decimal]] = {}
    for entry in entries:
        day = per_day.setdefault(entry.entry_date, [ZERO, ZERO, ZERO])
        if entry.entry_type == CASH_ENTRY_OPENING:
            opening += entry.amount
        elif entry.direction == CASH_DIRECTION_IN:
            receipts += entry.amount
            day[0] += entry.amount
        else:
            payments += entry.amount
            day[1] += entry.amount
        day[2] = entry.balance_after

    closings, _ = list_cash_closings(
        session=session,
        cash_book_id=book.id,
        date_from=date_from,
        date_to=date_to,
        limit=100_000,
    )
    return CashReport(
        book=book,
        date_from=date_from,
        date_to=date_to,
        opening_balance=opening,
        total_receipts=receipts,
        total_payments=payments,
        closing_balance=opening + receipts - payments,
        days=tuple(
            CashReportDay(day=day, receipts=values[0], payments=values[1], balance=values[2])
            for day, values in per_day.items()
        ),
        entries=entries,
        closings=tuple(reversed(closings)),
        reversed_by=reversal_numbers(session, [entry.id for entry in entries]),
    )


def cash_report_file_name(report: CashReport) -> str:
    start = report.date_from.isoformat() if report.date_from else "anfang"
    end = report.date_to.isoformat() if report.date_to else business_today().isoformat()
    return f"kassenbericht-{report.book.id}-{start}-{end}.csv"


def cash_report_csv(report: CashReport) -> str:
    """Kassenbericht als CSV; ``Satzart`` trennt Bestände, Einträge und Tagessalden."""
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Satzart",
            "Nr",
            "Datum",
            "Art",
            "Beschreibung",
            "Gegenkonto",
            "Steuercode",
            "Partner",
            "Beleg",
            "Buchung",
            "Einnahme",
            "Ausgabe",
            "Bestand",
            "Storno von",
            "Storniert durch",
        ]
    )

    def balance_row(kind: str, day: date | None, *, receipts="", payments="", balance) -> None:
        writer.writerow(
            [kind, "", day.isoformat() if day else "", "", "", "", "", "", "", ""]
            + [receipts, payments, f"{balance:.2f}", "", ""]
        )

    days = {day.day: day for day in report.days}
    balance_row("Anfangsbestand", report.date_from, balance=report.opening_balance)
    for index, entry in enumerate(report.entries):
        is_opening = entry.entry_type == CASH_ENTRY_OPENING
        incoming = entry.direction == CASH_DIRECTION_IN
        writer.writerow(
            [
                "Eintrag",
                entry.entry_number,
                entry.entry_date.isoformat(),
                ENTRY_TYPE_LABELS.get(entry.entry_type, entry.entry_type),
                entry.description,
                entry.contra_account.code if entry.contra_account else "",
                entry.tax_code.code if entry.tax_code else "",
                entry.partner.name if entry.partner else "",
                entry.document.file_name if entry.document else "",
                entry.journal_entry.posting_number if entry.journal_entry else "",
                f"{entry.amount:.2f}" if incoming and not is_opening else "",
                f"{entry.amount:.2f}" if not incoming else "",
                f"{entry.balance_after:.2f}",
                entry.reversal_of.entry_number if entry.reversal_of else "",
                report.reversed_by.get(entry.id, ""),
            ]
        )
        next_entry = report.entries[index + 1] if index + 1 < len(report.entries) else None
        if next_entry is None or next_entry.entry_date != entry.entry_date:
            day = days[entry.entry_date]
            balance_row(
                "Tagessaldo",
                entry.entry_date,
                receipts=f"{day.receipts:.2f}",
                payments=f"{day.payments:.2f}",
                balance=day.balance,
            )
    balance_row(
        "Endbestand",
        report.date_to,
        receipts=f"{report.total_receipts:.2f}",
        payments=f"{report.total_payments:.2f}",
        balance=report.closing_balance,
    )
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class CashIntegrityIssue:
    code: str
    message: str
    severity: str = SEVERITY_ERROR
    entry_id: int | None = None
    entry_number: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "entry_id": self.entry_id,
            "entry_number": self.entry_number,
        }


@dataclass(frozen=True, slots=True)
class CashBookIntegrityResult:
    """``valid`` meint Kette, Nummern, Bestand und Abschlüsse.

    Eine Abweichung zum Kontensaldo ist nur eine Warnung: Sie zeigt Buchungen am
    Kassenbuch vorbei, das Kassenbuch selbst bleibt intakt und benutzbar.
    """

    cash_book_id: int
    valid: bool
    checked_entries: int
    checked_closings: int
    head_hash: str | None
    book_balance: Decimal
    account_balance: Decimal
    account_matches: bool
    unlinked_postings: tuple[dict[str, Any], ...]
    issues: tuple[CashIntegrityIssue, ...]

    @property
    def errors(self) -> tuple[CashIntegrityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == SEVERITY_ERROR)

    @property
    def warnings(self) -> tuple[CashIntegrityIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == SEVERITY_WARNING)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cash_book_id": self.cash_book_id,
            "valid": self.valid,
            "checked_entries": self.checked_entries,
            "checked_closings": self.checked_closings,
            "head_hash": self.head_hash,
            "book_balance": f"{self.book_balance:.2f}",
            "account_balance": f"{self.account_balance:.2f}",
            "account_difference": f"{self.account_balance - self.book_balance:.2f}",
            "account_matches": self.account_matches,
            "unlinked_postings": list(self.unlinked_postings),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _check_entry_chain(entries: list[CashEntry]) -> list[CashIntegrityIssue]:
    """Nummern, Hashkette, Bestandsfortschreibung, Chronologie und Art/Richtung."""
    issues: list[CashIntegrityIssue] = []
    previous: CashEntry | None = None
    for expected_number, entry in enumerate(entries, start=1):
        context = {"entry_id": entry.id, "entry_number": entry.entry_number}
        if entry.entry_number != expected_number:
            issues.append(
                CashIntegrityIssue(
                    code="number_gap",
                    message=(
                        f"Lücke in der Nummernfolge: Erwartet Nr. {expected_number}, "
                        f"gefunden Nr. {entry.entry_number}."
                    ),
                    **context,
                )
            )
        expected_previous_hash = previous.content_hash if previous else GENESIS_HASH
        if entry.previous_hash != expected_previous_hash:
            issues.append(
                CashIntegrityIssue(
                    code="previous_hash_mismatch",
                    message=f"Eintrag Nr. {entry.entry_number}: Vorgänger-Hash passt nicht.",
                    **context,
                )
            )
        if entry.content_hash != calculate_cash_entry_hash(entry):
            issues.append(
                CashIntegrityIssue(
                    code="content_hash_mismatch",
                    message=f"Eintrag Nr. {entry.entry_number}: Inhalt passt nicht zum Hash.",
                    **context,
                )
            )
        previous_balance = previous.balance_after if previous else ZERO
        if entry.balance_after != previous_balance + _signed(entry):
            issues.append(
                CashIntegrityIssue(
                    code="balance_mismatch",
                    message=(
                        f"Eintrag Nr. {entry.entry_number}: Bestand "
                        f"{_euro(entry.balance_after)} folgt nicht aus dem Vorbestand "
                        f"{_euro(previous_balance)}."
                    ),
                    **context,
                )
            )
        if entry.balance_after < ZERO:
            issues.append(
                CashIntegrityIssue(
                    code="negative_balance",
                    message=f"Eintrag Nr. {entry.entry_number}: Der Bestand ist negativ.",
                    **context,
                )
            )
        if previous is not None and entry.entry_date < previous.entry_date:
            issues.append(
                CashIntegrityIssue(
                    code="date_out_of_order",
                    message=f"Eintrag Nr. {entry.entry_number} liegt vor seinem Vorgänger.",
                    **context,
                )
            )
        fixed_direction = _FIXED_DIRECTIONS.get(entry.entry_type)
        if fixed_direction is not None and entry.direction != fixed_direction:
            issues.append(
                CashIntegrityIssue(
                    code="direction_mismatch",
                    message=f"Eintrag Nr. {entry.entry_number}: Art und Richtung passen nicht.",
                    **context,
                )
            )
        previous = entry
    return issues


def _booked_journal_entry_ids(book: CashBook):
    return select(CashEntry.journal_entry_id).where(
        CashEntry.cash_book_id == book.id, CashEntry.journal_entry_id.is_not(None)
    )


def _check_journal_links(
    session: Session, book: CashBook, entries: list[CashEntry]
) -> list[CashIntegrityIssue]:
    """Die Buchung jedes Eintrags muss das Kassenkonto genau um den Betrag bewegen."""
    rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .where(
            JournalEntryLine.account_id == book.account_id,
            JournalEntryLine.journal_entry_id.in_(_booked_journal_entry_ids(book)),
        )
        .group_by(JournalEntryLine.journal_entry_id)
    ).all()
    moved = {row[0]: _to_cents(row[1]) - _to_cents(row[2]) for row in rows}

    issues: list[CashIntegrityIssue] = []
    for entry in entries:
        if entry.journal_entry_id is None:
            if entry.entry_type != CASH_ENTRY_OPENING:
                issues.append(
                    CashIntegrityIssue(
                        code="journal_entry_missing",
                        message=f"Eintrag Nr. {entry.entry_number} hat keine Buchung.",
                        entry_id=entry.id,
                        entry_number=entry.entry_number,
                    )
                )
            continue
        if moved.get(entry.journal_entry_id, ZERO) != _signed(entry):
            issues.append(
                CashIntegrityIssue(
                    code="journal_entry_mismatch",
                    message=(
                        f"Eintrag Nr. {entry.entry_number}: Die Buchung bewegt das Kassenkonto "
                        f"nicht um {_euro(_signed(entry))}."
                    ),
                    entry_id=entry.id,
                    entry_number=entry.entry_number,
                )
            )
    return issues


def _check_closings(
    entries: list[CashEntry], closings: list[CashClosing]
) -> list[CashIntegrityIssue]:
    """Soll, Differenz und Differenz-Eintrag jedes Kassensturzes gegen die Einträge."""
    issues: list[CashIntegrityIssue] = []
    entries_by_id = {entry.id: entry for entry in entries}
    expected = ZERO
    index = 0
    for closing in closings:
        label = f"Kassensturz {_german_date(closing.closing_date)}"
        # Das Soll ist der Bestand aller Einträge bis zum Datum, ohne die eigene Differenz.
        while (
            index < len(entries)
            and entries[index].entry_date <= closing.closing_date
            and entries[index].id != closing.difference_entry_id
        ):
            expected = entries[index].balance_after
            index += 1
        if closing.expected_balance != expected:
            issues.append(
                CashIntegrityIssue(
                    code="closing_expected_mismatch",
                    message=(
                        f"{label}: Soll-Bestand {_euro(closing.expected_balance)} passt nicht "
                        f"zu den Einträgen ({_euro(expected)})."
                    ),
                )
            )
        if closing.counted_balance - closing.expected_balance != closing.difference:
            issues.append(
                CashIntegrityIssue(
                    code="closing_difference_mismatch",
                    message=f"{label}: Die Differenz passt nicht zu Soll und gezähltem Bestand.",
                )
            )
        difference_entry = entries_by_id.get(closing.difference_entry_id)
        if (closing.difference == ZERO) != (difference_entry is None) or (
            difference_entry is not None and _signed(difference_entry) != closing.difference
        ):
            issues.append(
                CashIntegrityIssue(
                    code="closing_entry_mismatch",
                    message=f"{label}: Der Differenz-Eintrag passt nicht zur Differenz.",
                )
            )
    return issues


def _unlinked_postings(
    session: Session, book: CashBook, entries: list[CashEntry]
) -> list[dict[str, Any]]:
    """Buchungen auf dem Kassenkonto ohne Eintrag (ohne den übernommenen Anfangsbestand)."""
    stmt = (
        select(JournalEntry)
        .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .where(
            JournalEntryLine.account_id == book.account_id,
            JournalEntry.source != SOURCE_CARRYFORWARD,
            JournalEntry.id.not_in(_booked_journal_entry_ids(book)),
        )
        .distinct()
        .order_by(JournalEntry.entry_date, JournalEntry.id)
        .limit(UNLINKED_POSTINGS_LIMIT)
    )
    opening = next((e for e in entries if e.entry_type == CASH_ENTRY_OPENING), None)
    if opening is not None and opening.journal_entry_id is None:
        stmt = stmt.where(JournalEntry.entry_date > opening.entry_date)
    return [
        {
            "journal_entry_id": entry.id,
            "posting_number": entry.posting_number,
            "entry_date": entry.entry_date.isoformat(),
            "description": entry.description,
        }
        for entry in session.execute(stmt).scalars().all()
    ]


def verify_cash_book_integrity(*, session: Session, cash_book_id: int) -> CashBookIntegrityResult:
    """Prüft Hashkette, lückenlose Nummern, Bestand, Abschlüsse und den Kontensaldo."""
    book = session.get(CashBook, cash_book_id)
    if book is None:
        raise CashBookError("Kasse nicht gefunden.")
    entries = list(
        session.execute(
            select(CashEntry)
            .where(CashEntry.cash_book_id == book.id)
            .order_by(CashEntry.entry_number)
        )
        .scalars()
        .all()
    )
    closings = list(
        session.execute(
            select(CashClosing)
            .where(CashClosing.cash_book_id == book.id)
            .order_by(CashClosing.closing_date)
        )
        .scalars()
        .all()
    )

    issues = _check_entry_chain(entries)
    highest_number = entries[-1].entry_number if entries else 0
    if book.last_entry_number != highest_number:
        issues.append(
            CashIntegrityIssue(
                code="number_counter_mismatch",
                message=(
                    f"Der Nummernkreis steht auf Nr. {book.last_entry_number}, der letzte "
                    f"Eintrag ist Nr. {highest_number}: Am Ende der Kette fehlen Einträge."
                ),
            )
        )
    issues.extend(_check_journal_links(session, book, entries))
    issues.extend(_check_closings(entries, closings))

    book_balance = entries[-1].balance_after if entries else ZERO
    balance_on_account = account_balance(session, book.account_id)
    matches = balance_on_account == book_balance
    unlinked = _unlinked_postings(session, book, entries)
    if not matches:
        issues.append(
            CashIntegrityIssue(
                code="account_balance_mismatch",
                severity=SEVERITY_WARNING,
                message=(
                    f"Kassenbestand {_euro(book_balance)}, Saldo auf dem Kassenkonto "
                    f"{_euro(balance_on_account)}: Es gibt Buchungen am Kassenbuch vorbei. "
                    "Das Kassenbuch bleibt benutzbar; die Buchungen sollten geklärt werden."
                ),
            )
        )
    return CashBookIntegrityResult(
        cash_book_id=book.id,
        valid=not any(issue.severity == SEVERITY_ERROR for issue in issues),
        checked_entries=len(entries),
        checked_closings=len(closings),
        head_hash=entries[-1].content_hash if entries else None,
        book_balance=book_balance,
        account_balance=balance_on_account,
        account_matches=matches,
        unlinked_postings=tuple(unlinked),
        issues=tuple(issues),
    )
