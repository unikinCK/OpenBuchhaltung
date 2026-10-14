"""Buchungsvorschlag je offenem Bankumsatz (Gegenkonto, Steuercode, Kontierung, Partner).

Quellen in dieser Reihenfolge:

1. ``rule`` – passende Auto-Kontierungsregel (längstes Muster gewinnt).
2. ``transfer`` – Übertrag zwischen eigenen Bankkonten → Geldtransit-Konto.
3. ``history`` – letzte Bankbuchung derselben Gegenseite mit gleichem Vorzeichen
   („wie zuletzt“); Gegenkonto, Steuercode, Kontierung und Partner werden aus deren
   Buchung übernommen.

Grundlage für die Vorbelegung des Buchungsdialogs in Weboberfläche und App sowie für
``GET /api/v1/bank-transactions?include_suggestions=1`` (Feld ``booking_proposal``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.bank_import import detect_transfer_counterparts, find_geldtransit_account
from app.services.bank_rules import match_rules_for
from domain.models import Account, BankTransaction, JournalEntryLine, TaxCode


@dataclass(frozen=True, slots=True)
class BookingProposal:
    source: str  # rule | transfer | history
    contra_account: Account
    tax_code: TaxCode | None = None
    cost_center_id: int | None = None
    profit_center_id: int | None = None
    partner_id: int | None = None
    rule_pattern: str | None = None
    history_transaction_id: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "contra_account_id": self.contra_account.id,
            "contra_account_code": self.contra_account.code,
            "contra_account_name": self.contra_account.name,
            "tax_code_id": self.tax_code.id if self.tax_code else None,
            "tax_code": self.tax_code.code if self.tax_code else None,
            "cost_center_id": self.cost_center_id,
            "profit_center_id": self.profit_center_id,
            "partner_id": self.partner_id,
            "rule_pattern": self.rule_pattern,
            "history_transaction_id": self.history_transaction_id,
        }


def _history_key(transaction: BankTransaction) -> str | None:
    """Gegenseite (sonst Verwendungszweck) ohne Groß-/Kleinschreibung."""
    text = (transaction.counterparty or "").strip() or (transaction.purpose or "").strip()
    return text.lower() or None


def _sign(transaction: BankTransaction) -> int:
    return 1 if transaction.amount > 0 else -1


def _history_proposals(
    session: Session, transactions: Sequence[BankTransaction]
) -> dict[int, BookingProposal]:
    wanted: dict[tuple[int, str, int], list[BankTransaction]] = {}
    for transaction in transactions:
        key = _history_key(transaction)
        if key:
            wanted.setdefault((transaction.company_id, key, _sign(transaction)), []).append(
                transaction
            )
    if not wanted:
        return {}

    company_ids = {company_id for company_id, _key, _sign_ in wanted}
    keys = {key for _company, key, _sign_ in wanted}
    key_expr = func.lower(
        func.coalesce(func.nullif(func.trim(BankTransaction.counterparty), ""),
                      func.trim(BankTransaction.purpose))
    )
    booked = session.execute(
        select(BankTransaction)
        .where(
            BankTransaction.company_id.in_(company_ids),
            BankTransaction.status == "booked",
            BankTransaction.journal_entry_id.is_not(None),
            key_expr.in_(keys),
        )
        .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
    ).scalars()

    latest: dict[tuple[int, str, int], BankTransaction] = {}
    for previous in booked:
        key = (previous.company_id, _history_key(previous), _sign(previous))
        if key in wanted and key not in latest:
            latest[key] = previous
    if not latest:
        return {}

    vat_accounts = set(
        session.execute(
            select(TaxCode.vat_account_id).where(
                TaxCode.company_id.in_(company_ids), TaxCode.vat_account_id.is_not(None)
            )
        ).scalars()
    )
    proposals: dict[int, BookingProposal] = {}
    for key, previous in latest.items():
        lines = session.execute(
            select(JournalEntryLine)
            .where(JournalEntryLine.journal_entry_id == previous.journal_entry_id)
            .order_by(JournalEntryLine.id)
        ).scalars().all()
        contra = [
            line
            for line in lines
            if line.account_id != previous.bank_account_id and line.account_id not in vat_accounts
        ]
        if len(contra) != 1:
            continue  # Splitbuchung o. Ä.: kein eindeutiger Vorschlag
        line = contra[0]
        account = session.get(Account, line.account_id)
        if account is None or not account.is_active:
            continue
        tax_code = session.get(TaxCode, line.tax_code_id) if line.tax_code_id else None
        if tax_code is not None and not tax_code.is_active:
            tax_code = None
        partner_id = line.partner_id or next(
            (other.partner_id for other in lines if other.partner_id), None
        )
        proposal = BookingProposal(
            source="history",
            contra_account=account,
            tax_code=tax_code,
            cost_center_id=line.cost_center_id,
            profit_center_id=line.profit_center_id,
            partner_id=partner_id,
            history_transaction_id=previous.id,
        )
        for transaction in wanted[key]:
            proposals[transaction.id] = proposal
    return proposals


def booking_proposals(
    *, session: Session, transactions: Sequence[BankTransaction]
) -> dict[int, BookingProposal]:
    """Vorschlag je offenem Umsatz (Regel vor Geldtransit vor Historie)."""
    open_transactions = [t for t in transactions if t.status == "open"]
    if not open_transactions:
        return {}

    proposals = _history_proposals(session, open_transactions)

    transfers = detect_transfer_counterparts(session=session, transactions=open_transactions)
    geldtransit_by_company: dict[int, Account | None] = {}
    for transaction in open_transactions:
        if transaction.id not in transfers:
            continue
        if transaction.company_id not in geldtransit_by_company:
            geldtransit_by_company[transaction.company_id] = find_geldtransit_account(
                session=session, company_id=transaction.company_id
            )
        account = geldtransit_by_company[transaction.company_id]
        if account is not None:
            proposals[transaction.id] = BookingProposal(source="transfer", contra_account=account)

    for transaction_id, rule in match_rules_for(
        session=session, transactions=open_transactions
    ).items():
        proposals[transaction_id] = BookingProposal(
            source="rule",
            contra_account=rule.contra_account,
            tax_code=rule.tax_code,
            cost_center_id=rule.cost_center_id,
            profit_center_id=rule.profit_center_id,
            rule_pattern=rule.pattern,
        )
    return proposals
