"""Dashboard-Kennzahlen: offene Aufgaben, Bankstände und Ergebnis einer Gesellschaft.

Gemeinsame Grundlage für die Dashboard-Seite, ``GET /api/v1/dashboard`` und das
MCP-Tool ``get_dashboard`` (u. a. Startbildschirm und Widgets der iOS-App).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.journal_entries import SOURCE_CARRYFORWARD
from app.services.journal_templates import due_templates
from app.services.reports import (
    balance_sheet_for_company,
    fiscal_year_for_date,
    income_statement_for_company,
)
from app.services.scoping import scoped_select
from domain.models import (
    Account,
    BankTransaction,
    Document,
    FinTSConnection,
    JournalEntry,
    JournalEntryLine,
    OpenBankingConnection,
    OpenItem,
    ReceiptMatchSuggestion,
)

ZERO = Decimal("0.00")

# Geldkonten (Kasse, Postbank, Bank) je Kontenrahmen. Geldtransit (SKR03 1360,
# SKR04 1460) liegt außerhalb: ein Verrechnungskonto, kein Bankkonto.
MONEY_ACCOUNT_RANGES = {"skr03": (1000, 1299), "skr04": (1600, 1899)}


def count_rows(session: Session, stmt) -> int:
    return session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()


def open_task_counts(session: Session, company_id: int) -> dict[str, int]:
    """Offene Aufgaben einer Gesellschaft (Dashboard-Kacheln, App-Inbox)."""
    return {
        "open_bank_transactions": count_rows(
            session,
            scoped_select(BankTransaction, company_id=company_id).where(
                BankTransaction.status == "open"
            ),
        ),
        # Nur aktuelle Fassungen: Eine ersetzte Version bleibt sonst für immer
        # „ohne Buchung“, auch wenn ihre Nachfolgerin längst verknüpft ist.
        "unlinked_documents": count_rows(
            session,
            scoped_select(Document, company_id=company_id).where(
                Document.journal_entry_id.is_(None),
                Document.is_current.is_(True),
            ),
        ),
        "overdue_open_items": count_rows(
            session,
            scoped_select(OpenItem, company_id=company_id).where(
                OpenItem.status == "open",
                OpenItem.due_date.is_not(None),
                OpenItem.due_date < date.today(),
            ),
        ),
        "pending_match_suggestions": count_rows(
            session,
            scoped_select(ReceiptMatchSuggestion, company_id=company_id).where(
                ReceiptMatchSuggestion.status == "offen"
            ),
        ),
        "due_templates": len(due_templates(session=session, company_id=company_id)),
    }


def _in_money_range(code: str, chart: str) -> bool:
    low, high = MONEY_ACCOUNT_RANGES[chart]
    return len(code) == 4 and code.isascii() and code.isdigit() and low <= int(code) <= high


def money_account_ids(session: Session, company_id: int) -> set[int]:
    """Konten, die als Bankkonten im Dashboard stehen.

    Das sind Konten mit importierten Umsätzen oder aktiver Bankverbindung (FinTS,
    Open Banking) und – bei erkennbarem Kontenrahmen – bebuchte, aktive Aktivkonten
    im Bereich der Geldkonten (SKR03 1000–1299, SKR04 1600–1899), etwa Tages- und
    Festgeld ohne Kontoauszugsimport. Ohne erkennbaren Kontenrahmen beweist die
    Nummer nichts (SKR04 1200 sind Forderungen); dann zählen nur die ersten beiden.
    """
    ids = set(
        session.execute(
            select(BankTransaction.bank_account_id)
            .where(BankTransaction.company_id == company_id)
            .distinct()
        ).scalars()
    )
    for connection in (FinTSConnection, OpenBankingConnection):
        ids.update(
            session.execute(
                select(connection.bank_account_id).where(
                    connection.company_id == company_id, connection.is_active.is_(True)
                )
            ).scalars()
        )
    chart = detect_company_chart(session=session, company_id=company_id)
    if chart in MONEY_ACCOUNT_RANGES:
        posted = (
            select(JournalEntryLine.account_id)
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .where(JournalEntry.company_id == company_id)
        )
        candidates = session.execute(
            select(Account).where(
                Account.company_id == company_id,
                Account.is_active.is_(True),
                Account.account_type == "asset",
                Account.subledger.is_(None),
                Account.id.in_(posted),
            )
        ).scalars()
        ids.update(account.id for account in candidates if _in_money_range(account.code, chart))
    return ids


def bank_account_rows(session: Session, company_id: int) -> list[dict[str, object]]:
    """Je Geldkonto: Buchsaldo, Summe der importierten Umsätze und letzte Bewegungen.

    Der Buchsaldo ist Soll − Haben aller Buchungen ohne Saldovorträge (die
    wiederholen nur die Vorjahre, deren Buchungen ohnehin mitzählen).
    """
    account_ids = money_account_ids(session, company_id)
    if not account_ids:
        return []
    book = {
        account_id: (Decimal(debit) - Decimal(credit), latest)
        for account_id, debit, credit, latest in session.execute(
            select(
                JournalEntryLine.account_id,
                func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
                func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
                func.max(JournalEntry.entry_date),
            )
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .where(
                JournalEntry.company_id == company_id,
                JournalEntry.source != SOURCE_CARRYFORWARD,
                JournalEntryLine.account_id.in_(account_ids),
            )
            .group_by(JournalEntryLine.account_id)
        ).all()
    }
    statements = {
        row[0]: row[1:]
        for row in session.execute(
            select(
                BankTransaction.bank_account_id,
                func.count(),
                func.coalesce(func.sum(BankTransaction.amount), 0),
                func.sum(case((BankTransaction.status == "open", 1), else_=0)),
                func.max(BankTransaction.booking_date),
            )
            .where(
                BankTransaction.company_id == company_id,
                BankTransaction.bank_account_id.in_(account_ids),
            )
            .group_by(BankTransaction.bank_account_id)
        ).all()
    }
    accounts = session.execute(
        select(Account).where(Account.id.in_(account_ids)).order_by(Account.code)
    ).scalars()
    rows = []
    for account in accounts:
        book_balance, latest_entry = book.get(account.id, (ZERO, None))
        count, total, open_count, latest_booking = statements.get(account.id, (0, 0, 0, None))
        rows.append(
            {
                "account_id": account.id,
                "account_code": account.code,
                "account_name": account.name,
                "book_balance": Decimal(book_balance).quantize(ZERO),
                "statement_total": Decimal(total).quantize(ZERO),
                "transaction_count": int(count),
                "open_transactions": int(open_count or 0),
                "latest_booking_date": latest_booking,
                "latest_entry_date": latest_entry,
            }
        )
    return rows


def dashboard_summary(
    *, session: Session, company_id: int, as_of: date | None = None
) -> dict[str, object]:
    """Kompakte Übersicht für mobile Clients.

    Das Ergebnis läuft vom Beginn des Geschäftsjahres, das ``as_of`` enthält, bis
    ``as_of`` (ohne Geschäftsjahr: alle Buchungen bis ``as_of``). Bankstände sind
    die Buchsalden der Geldkonten (siehe :func:`money_account_ids`), daneben die
    Summe der Kontoauszugszeilen.
    """
    as_of = as_of or date.today()
    fiscal_year = fiscal_year_for_date(session, company_id, as_of)
    income = income_statement_for_company(
        session=session,
        company_id=company_id,
        date_from=fiscal_year.start_date if fiscal_year else None,
        date_to=as_of,
    )["totals"]
    balance = balance_sheet_for_company(session=session, company_id=company_id, date_to=as_of)[
        "totals"
    ]
    return {
        "as_of": as_of,
        "fiscal_year": fiscal_year.label if fiscal_year else None,
        "tasks": open_task_counts(session, company_id),
        "bank_accounts": bank_account_rows(session, company_id),
        "income_statement": income,
        "balance_sheet": {
            "total_assets": balance["total_assets"],
            "total_liabilities_and_equity": balance["total_liabilities_and_equity"],
        },
    }
