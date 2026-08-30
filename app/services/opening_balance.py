"""Eröffnungsbilanz/Saldenübernahme beim Umstieg von einem Altsystem.

Die übergebenen Kontensalden werden als eine Eröffnungsbuchung erfasst;
eine verbleibende Differenz wird automatisch auf das Saldenvortragskonto
gebucht (Konto 9000/9008/9009 oder Name „Saldenvortrag“), damit die
Buchung aufgeht und der Vortrag nachvollziehbar bleibt.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, Company, JournalEntry

CARRYFORWARD_CODES = ("9000", "9008", "9009")


class OpeningBalanceError(ValueError):
    """Raised when an opening balance cannot be booked."""


def find_carryforward_account(*, session: Session, company_id: int) -> Account | None:
    return (
        session.execute(
            select(Account)
            .where(
                Account.company_id == company_id,
                Account.is_active.is_(True),
                or_(
                    Account.code.in_(CARRYFORWARD_CODES),
                    Account.name.ilike("%saldenvortr%"),
                ),
            )
            .order_by(Account.code)
        )
        .scalars()
        .first()
    )


def book_opening_balance(
    *,
    session: Session,
    company_id: int,
    entry_date: date,
    balances: list[dict],
    changed_by: str,
    description: str = "Eröffnungsbilanz / Saldenübernahme",
) -> JournalEntry:
    """Bucht die Saldenübernahme; ``balances`` sind Dicts mit ``account_id``
    oder ``account_code`` plus ``debit``/``credit`` (eine Seite > 0)."""
    company = session.get(Company, company_id)
    if company is None:
        raise OpeningBalanceError("Gesellschaft nicht gefunden.")
    if not balances:
        raise OpeningBalanceError("Keine Salden übergeben.")

    accounts_by_code = {
        account.code: account
        for account in session.execute(
            select(Account).where(Account.company_id == company.id)
        ).scalars()
    }
    accounts_by_id = {account.id: account for account in accounts_by_code.values()}

    lines: list[JournalLineInput] = []
    total = Decimal("0.00")
    zero = Decimal("0.00")
    for index, raw in enumerate(balances, start=1):
        account = None
        if raw.get("account_id"):
            account = accounts_by_id.get(int(raw["account_id"]))
        elif raw.get("account_code"):
            account = accounts_by_code.get(str(raw["account_code"]).strip())
        if account is None:
            raise OpeningBalanceError(f"Zeile {index}: Konto nicht gefunden.")

        try:
            debit = Decimal(str(raw.get("debit") or "0")).quantize(zero)
            credit = Decimal(str(raw.get("credit") or "0")).quantize(zero)
        except InvalidOperation as exc:
            raise OpeningBalanceError(f"Zeile {index}: ungültiger Betrag.") from exc
        if debit < 0 or credit < 0 or (debit > 0 and credit > 0) or debit == credit == zero:
            raise OpeningBalanceError(
                f"Zeile {index}: genau eine Seite (Soll oder Haben) muss größer 0 sein."
            )

        total += debit - credit
        lines.append(
            JournalLineInput(
                account_id=account.id, debit_amount=debit, credit_amount=credit
            )
        )

    if total != zero:
        carryforward = find_carryforward_account(session=session, company_id=company.id)
        if carryforward is None:
            raise OpeningBalanceError(
                "Die Salden gehen nicht auf und es gibt kein Saldenvortragskonto "
                "(Kontonummer 9000 oder Name „Saldenvortrag“) — bitte unter Konten "
                "anlegen oder die Differenz selbst ausgleichen."
            )
        lines.append(
            JournalLineInput(
                account_id=carryforward.id,
                debit_amount=-total if total < zero else zero,
                credit_amount=total if total > zero else zero,
                description="Saldenvortrag (automatischer Ausgleich)",
            )
        )

    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description=description,
            status="posted",
            changed_by=changed_by,
            # Salden sind Bruttowerte aus dem Altsystem — keine Auto-Steuerzeilen.
            expand_tax_lines=False,
            lines=lines,
        ),
    )


def parse_balance_csv(text: str) -> list[dict]:
    """Parst „Konto;Soll;Haben“-Zeilen (Kopfzeile optional, deutsches oder
    englisches Zahlenformat über den Bank-Import-Betragsparser)."""
    from app.services.bank_import import BankImportError, parse_amount

    def amount(raw: str, line_number: int) -> str:
        raw = raw.strip()
        if not raw:
            return "0"
        try:
            return str(parse_amount(raw))
        except BankImportError as exc:
            raise OpeningBalanceError(f"Zeile {line_number}: {exc}") from exc

    balances = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.replace("\t", ";").split(";")]
        if parts and parts[0].lower() in {"konto", "kontonummer", "account"}:
            continue
        if len(parts) < 2:
            raise OpeningBalanceError(
                f"Zeile {line_number}: erwartet „Konto;Soll;Haben“."
            )
        while len(parts) < 3:
            parts.append("")
        balances.append(
            {
                "account_code": parts[0],
                "debit": amount(parts[1], line_number),
                "credit": amount(parts[2], line_number),
            }
        )
    return balances
