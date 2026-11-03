"""Partnerkonto und Saldenliste Debitoren/Kreditoren (Nebenbuch).

Es zählen nur Zeilen auf Sammelkonten (ADR-002, Nachtrag 2026-10-12); Partnerbezüge
auf anderen Konten erscheinen in der Buchungsliste des Partners. Salden sind Soll minus
Haben wie im Kontoblatt. Saldovorträge bleiben außen vor, weil die kumulierten Zeilen
die Vorjahre bereits enthalten.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.journal_entries import SOURCE_CARRYFORWARD
from app.services.open_items import ITEM_TYPE_SUBLEDGER
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    Account,
    BusinessPartner,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
)

ZERO = Decimal("0.00")
DEFAULT_STATEMENT_LIMIT = 200
MAX_STATEMENT_LIMIT = 1000
SUBLEDGER_SIDES = (SUBLEDGER_DEBTOR, SUBLEDGER_CREDITOR)
SIDE_LABELS = {SUBLEDGER_DEBTOR: "Debitoren", SUBLEDGER_CREDITOR: "Kreditoren"}
ITEM_TYPE_BY_SIDE = {side: item_type for item_type, side in ITEM_TYPE_SUBLEDGER.items()}


class PartnerLedgerError(ValueError):
    """Ungültige Parameter für Partnerkonto oder Saldenliste."""


def _check_side(side: str | None) -> None:
    if side is not None and side not in SUBLEDGER_SIDES:
        raise PartnerLedgerError(f"side muss eine von {', '.join(SUBLEDGER_SIDES)} sein.")


def _partner_number(partner: BusinessPartner, side: str) -> str | None:
    return partner.debtor_number if side == SUBLEDGER_DEBTOR else partner.creditor_number


def _partner_sides(partner: BusinessPartner) -> list[str]:
    return [side for side in SUBLEDGER_SIDES if _partner_number(partner, side)]


def _statement_side(
    *,
    session: Session,
    partner: BusinessPartner,
    side: str,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    conditions = (
        JournalEntryLine.partner_id == partner.id,
        JournalEntry.company_id == partner.company_id,
        JournalEntry.source != SOURCE_CARRYFORWARD,
        Account.subledger == side,
    )
    joined = (
        select(
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(*conditions)
    )
    opening = ZERO
    if date_from is not None:
        debit, credit = session.execute(
            joined.where(JournalEntry.entry_date < date_from)
        ).one()
        opening = Decimal(debit) - Decimal(credit)

    stmt = (
        select(JournalEntryLine, JournalEntry, Account)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(*conditions)
        .order_by(JournalEntry.entry_date, JournalEntry.id, JournalEntryLine.line_number)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)

    balance = opening
    debit_total = credit_total = ZERO
    lines: list[dict[str, Any]] = []
    for line, entry, account in session.execute(stmt).all():
        balance += line.debit_amount - line.credit_amount
        debit_total += line.debit_amount
        credit_total += line.credit_amount
        lines.append(
            {
                "journal_entry_id": entry.id,
                "posting_number": entry.posting_number,
                "entry_date": entry.entry_date,
                "description": line.description or entry.description,
                "is_finalized": entry.is_finalized,
                "account_id": account.id,
                "account_code": account.code,
                "debit_amount": line.debit_amount,
                "credit_amount": line.credit_amount,
                "balance": balance,
            }
        )
    return {
        "side": side,
        "side_label": SIDE_LABELS[side],
        "number": _partner_number(partner, side),
        "opening_balance": opening,
        "debit_total": debit_total,
        "credit_total": credit_total,
        "closing_balance": balance,
        "line_count": len(lines),
        "lines": lines[offset : offset + limit],
    }


def partner_statement(
    *,
    session: Session,
    partner: BusinessPartner,
    side: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = DEFAULT_STATEMENT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Partnerkonto je Rolle (Debitor/Kreditor) und die offenen Posten des Partners."""
    _check_side(side)
    if date_from and date_to and date_from > date_to:
        raise PartnerLedgerError("date_from darf nicht nach date_to liegen.")
    limit = max(1, min(int(limit), MAX_STATEMENT_LIMIT))
    offset = max(0, int(offset))
    sides = [side] if side else _partner_sides(partner)
    open_items_stmt = (
        select(OpenItem)
        .where(OpenItem.partner_id == partner.id, OpenItem.status == "open")
        .order_by(OpenItem.due_date.is_(None), OpenItem.due_date, OpenItem.id)
    )
    if side:
        open_items_stmt = open_items_stmt.where(OpenItem.item_type == ITEM_TYPE_BY_SIDE[side])
    return {
        "date_from": date_from,
        "date_to": date_to,
        "limit": limit,
        "offset": offset,
        "sides": [
            _statement_side(
                session=session,
                partner=partner,
                side=current,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            )
            for current in sides
        ],
        "open_items": list(session.execute(open_items_stmt).scalars()),
    }


def _ledger_sums(
    session: Session, company_id: int, side: str, as_of: date
) -> list[tuple[int, int | None, Decimal, Decimal]]:
    """(Konto, Partner, Soll, Haben) der Sammelkonten einer Seite bis zum Stichtag."""
    rows = session.execute(
        select(
            JournalEntryLine.account_id,
            JournalEntryLine.partner_id,
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.source != SOURCE_CARRYFORWARD,
            JournalEntry.entry_date <= as_of,
            Account.subledger == side,
        )
        .group_by(JournalEntryLine.account_id, JournalEntryLine.partner_id)
    ).all()
    return [
        (account_id, partner_id, Decimal(debit), Decimal(credit))
        for account_id, partner_id, debit, credit in rows
    ]


def partner_balances(
    *, session: Session, company_id: int, side: str, as_of: date | None = None
) -> dict[str, Any]:
    """Saldenliste einer Seite zum Stichtag mit Abstimmung je Sammelkonto und OPOS-Vergleich.

    Je Sammelkonto: Summe der Partnersalden + Zeilen ohne Partner = Saldo des Sachkontos. Der
    OPOS-Vergleich zeigt offene Beträge von heute für Posten bis zum Stichtag; er ist nur für
    den aktuellen Stand scharf, weil Teilzahlungen nicht je Datum vorliegen.
    """
    _check_side(side)
    as_of = as_of or date.today()
    sums = _ledger_sums(session, company_id, side, as_of)

    accounts = {
        account.id: account
        for account in session.execute(
            select(Account).where(Account.company_id == company_id, Account.subledger == side)
        ).scalars()
    }
    account_rows = {
        account_id: {
            "account_id": account_id,
            "code": account.code,
            "name": account.name,
            "partner_total": ZERO,
            "unassigned": ZERO,
            "ledger_balance": ZERO,
        }
        for account_id, account in accounts.items()
    }
    partner_sums: dict[int, dict[str, Decimal]] = {}
    for account_id, partner_id, debit, credit in sums:
        balance = debit - credit
        row = account_rows[account_id]
        row["ledger_balance"] += balance
        if partner_id is None:
            row["unassigned"] += balance
            continue
        row["partner_total"] += balance
        totals = partner_sums.setdefault(partner_id, {"debit": ZERO, "credit": ZERO})
        totals["debit"] += debit
        totals["credit"] += credit

    open_by_partner = dict(
        session.execute(
            select(OpenItem.partner_id, func.coalesce(func.sum(OpenItem.open_amount), 0))
            .where(
                OpenItem.company_id == company_id,
                OpenItem.partner_id.is_not(None),
                OpenItem.status == "open",
                OpenItem.item_type == ITEM_TYPE_BY_SIDE[side],
                OpenItem.entry_date <= as_of,
            )
            .group_by(OpenItem.partner_id)
        ).all()
    )
    partner_ids = set(partner_sums) | set(open_by_partner)
    partners = (
        {
            partner.id: partner
            for partner in session.execute(
                select(BusinessPartner).where(BusinessPartner.id.in_(partner_ids))
            ).scalars()
        }
        if partner_ids
        else {}
    )

    # Debitoren stehen im Soll, Kreditoren im Haben: offene Posten sind immer positiv.
    sign = Decimal(1) if side == SUBLEDGER_DEBTOR else Decimal(-1)
    partner_rows = []
    for partner_id in partner_ids:
        partner = partners[partner_id]
        totals = partner_sums.get(partner_id, {"debit": ZERO, "credit": ZERO})
        balance = totals["debit"] - totals["credit"]
        open_total = Decimal(open_by_partner.get(partner_id, ZERO))
        partner_rows.append(
            {
                "partner_id": partner_id,
                "number": _partner_number(partner, side) or "",
                "name": partner.name,
                "is_active": partner.is_active,
                "debit_total": totals["debit"],
                "credit_total": totals["credit"],
                "balance": balance,
                "open_items_total": open_total,
                "opos_difference": sign * balance - open_total,
            }
        )
    partner_rows.sort(key=lambda row: (row["number"], row["name"]))

    account_list = sorted(account_rows.values(), key=lambda row: row["code"])
    return {
        "company_id": company_id,
        "side": side,
        "side_label": SIDE_LABELS[side],
        "as_of": as_of,
        "accounts": account_list,
        "partners": partner_rows,
        "totals": {
            "debit_total": sum((row["debit_total"] for row in partner_rows), ZERO),
            "credit_total": sum((row["credit_total"] for row in partner_rows), ZERO),
            "partner_balance": sum((row["balance"] for row in partner_rows), ZERO),
            "unassigned": sum((row["unassigned"] for row in account_list), ZERO),
            "ledger_balance": sum((row["ledger_balance"] for row in account_list), ZERO),
            "open_items_total": sum((row["open_items_total"] for row in partner_rows), ZERO),
            "opos_difference": sum((row["opos_difference"] for row in partner_rows), ZERO),
        },
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _json_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in row.items()}


def serialize_partner_balances(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": report["company_id"],
        "side": report["side"],
        "as_of": report["as_of"].isoformat(),
        "accounts": [_json_row(row) for row in report["accounts"]],
        "partners": [_json_row(row) for row in report["partners"]],
        "totals": _json_row(report["totals"]),
    }


def serialize_statement_side(side: dict[str, Any]) -> dict[str, Any]:
    return {
        **_json_row({key: value for key, value in side.items() if key != "lines"}),
        "lines": [_json_row(line) for line in side["lines"]],
    }
