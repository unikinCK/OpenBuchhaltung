"""Buchungsvorschlag je offenem Bankumsatz (Gegenkonto, Steuercode, Kontierung, Partner).

Quellen in dieser Reihenfolge:

1. ``rule`` – passende Auto-Kontierungsregel (längstes Muster gewinnt).
2. ``transfer`` – Übertrag zwischen eigenen Bankkonten → Geldtransit-Konto.
3. ``open_item`` – genau ein offener Posten passt nach Partner bzw. Referenz und Betrag.
4. ``history`` – letzte Bankbuchung derselben Gegenseite mit gleichem Vorzeichen
   („wie zuletzt“); Gegenkonto, Steuercode, Kontierung und Partner werden aus deren
   Buchung übernommen.

Daneben erkennt :func:`partner_matches` den Partner (IBAN, Partnernummer im Text, Name). Eine
eindeutige Erkennung geht vor der Historie, die nur am Namen hängt. Ein unsicherer Treffer
bleibt ein Vorschlag; er wird weder vorbelegt noch beim Regel-Lauf verwendet.

Grundlage für die Vorbelegung des Buchungsdialogs in Weboberfläche und App sowie für
``GET /api/v1/bank-transactions?include_suggestions=1`` (Felder ``booking_proposal``,
``partner_match`` und ``open_item_suggestions``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.services.bank_import import (
    BankImportError,
    book_transaction,
    detect_transfer_counterparts,
    find_geldtransit_account,
)
from app.services.bank_rules import match_rules_for
from app.services.open_items import settle_open_item
from app.services.partner_recognition import (
    PartnerDirectory,
    PartnerMatch,
    load_partner_directory,
    role_for_amount,
    transaction_signals,
)
from app.services.partners import partner_display_number
from domain.models import Account, BankTransaction, JournalEntryLine, OpenItem, TaxCode


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
    open_item_id: int | None = None

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
            "open_item_id": self.open_item_id,
        }


@dataclass(frozen=True, slots=True)
class OpenItemSuggestion:
    """Offener Posten, zu dem ein Umsatz nach Partner bzw. Referenz und Betrag passt."""

    item: OpenItem
    matched_on: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        item = self.item
        return {
            "open_item_id": item.id,
            "reference": item.reference,
            "item_type": item.item_type,
            "account_id": item.account_id,
            "partner_id": item.partner_id,
            "partner_name": item.partner.name if item.partner else None,
            "partner_number": partner_display_number(item.partner) if item.partner else None,
            "counterparty": item.counterparty,
            "open_amount": str(item.open_amount),
            "due_date": item.due_date.isoformat() if item.due_date else None,
            "matched_on": list(self.matched_on),
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


def partner_matches(
    *, session: Session, transactions: Sequence[BankTransaction]
) -> dict[int, PartnerMatch]:
    """Partnererkennung je offenem Umsatz; das Verzeichnis entsteht je Gesellschaft einmal."""
    directories: dict[int, PartnerDirectory] = {}
    matches: dict[int, PartnerMatch] = {}
    for transaction in transactions:
        if transaction.status != "open":
            continue
        directory = directories.get(transaction.company_id)
        if directory is None:
            directory = load_partner_directory(session, transaction.company_id)
            directories[transaction.company_id] = directory
        matches[transaction.id] = directory.match(
            transaction_signals(transaction),
            preferred_role=role_for_amount(transaction.amount),
        )
    return matches


def open_item_suggestions(
    *,
    session: Session,
    transactions: Sequence[BankTransaction],
    matches: Mapping[int, PartnerMatch],
) -> dict[int, list[OpenItemSuggestion]]:
    """Offene Posten je Umsatz: gleicher Betrag und passender Partner oder Referenz im Zweck.

    Kandidaten zählen mit, ein Treffer ist dadurch nicht zugeordnet: Gebucht wird erst, wenn
    jemand den Posten bestätigt.
    """
    open_transactions = [t for t in transactions if t.status == "open"]
    if not open_transactions:
        return {}
    items = (
        session.execute(
            select(OpenItem)
            .options(selectinload(OpenItem.partner))
            .where(
                OpenItem.company_id.in_({t.company_id for t in open_transactions}),
                OpenItem.status == "open",
                OpenItem.open_amount.in_({abs(t.amount) for t in open_transactions}),
            )
            .order_by(OpenItem.due_date, OpenItem.id)
        )
        .scalars()
        .all()
    )
    suggestions: dict[int, list[OpenItemSuggestion]] = {}
    for transaction in open_transactions:
        match = matches.get(transaction.id)
        partner_ids = (
            {candidate.partner.id for candidate in match.candidates} if match else set()
        )
        wanted_type = "receivable" if transaction.amount > 0 else "payable"
        purpose = (transaction.purpose or "").casefold()
        found: list[OpenItemSuggestion] = []
        for item in items:
            if (
                item.company_id != transaction.company_id
                or item.item_type != wanted_type
                or item.open_amount != abs(transaction.amount)
            ):
                continue
            matched_on: list[str] = []
            if item.partner_id is not None and item.partner_id in partner_ids:
                matched_on.append("partner")
            if len(item.reference) >= 4 and item.reference.casefold() in purpose:
                matched_on.append("reference")
            if matched_on:
                found.append(OpenItemSuggestion(item=item, matched_on=(*matched_on, "amount")))
        if found:
            # Die Referenz im Verwendungszweck ist der stärkere Beleg für genau diesen Posten.
            found.sort(key=lambda suggestion: "reference" not in suggestion.matched_on)
            suggestions[transaction.id] = found
    return suggestions


def book_transaction_to_open_item(
    *,
    session: Session,
    transaction_id: int,
    open_item_id: int,
    changed_by: str,
    description: str | None = None,
) -> OpenItem:
    """Bucht den Umsatz auf das Sammelkonto des Postens und gleicht den Posten damit aus.

    Der Posten bringt Konto und Partner mit. Vorab geprüft, denn ``book_transaction`` und
    ``settle_open_item`` committen getrennt.
    """
    transaction = session.get(BankTransaction, transaction_id)
    if transaction is None:
        raise BankImportError("Bankumsatz nicht gefunden.")
    item = session.get(OpenItem, open_item_id)
    if item is None or item.company_id != transaction.company_id:
        raise BankImportError("Offener Posten nicht gefunden.")
    if item.status != "open":
        raise BankImportError("Der offene Posten ist bereits ausgeglichen.")
    if (item.item_type == "receivable") != (transaction.amount > 0):
        raise BankImportError(
            "Forderungen gleichen Zahlungseingänge aus, Verbindlichkeiten Zahlungsausgänge."
        )
    if abs(transaction.amount) > item.open_amount:
        raise BankImportError("Der Umsatz ist höher als der offene Betrag des Postens.")
    # Bestehende Posten inaktiver Partner bleiben ausgleichbar, neu bebuchen lässt er sich nicht.
    partner = item.partner
    transaction = book_transaction(
        session=session,
        transaction_id=transaction.id,
        contra_account_id=item.account_id,
        partner_id=partner.id if partner is not None and partner.is_active else None,
        description=description or f"Bank: {item.reference} – {transaction.purpose}"[:500],
        changed_by=changed_by,
    )
    return settle_open_item(
        session=session,
        open_item_id=item.id,
        amount=abs(transaction.amount),
        bank_transaction_id=transaction.id,
        journal_entry_id=transaction.journal_entry_id,
        changed_by=changed_by,
    )


def booking_proposals(
    *,
    session: Session,
    transactions: Sequence[BankTransaction],
    matches: Mapping[int, PartnerMatch] | None = None,
    item_suggestions: Mapping[int, list[OpenItemSuggestion]] | None = None,
) -> dict[int, BookingProposal]:
    """Vorschlag je offenem Umsatz (Regel vor Geldtransit vor offenem Posten vor Historie)."""
    open_transactions = [t for t in transactions if t.status == "open"]
    if not open_transactions:
        return {}

    if matches is None:
        matches = partner_matches(session=session, transactions=open_transactions)
    if item_suggestions is None:
        item_suggestions = open_item_suggestions(
            session=session, transactions=open_transactions, matches=matches
        )

    proposals = _history_proposals(session, open_transactions)
    for transaction_id, proposal in list(proposals.items()):
        match = matches.get(transaction_id)
        if match is not None and match.partner is not None:
            proposals[transaction_id] = replace(proposal, partner_id=match.partner.id)

    for transaction in open_transactions:
        found = item_suggestions.get(transaction.id, [])
        # Mehrere passende Posten: Der Mensch wählt, das Gegenkonto bleibt ohne Vorschlag.
        if len(found) != 1:
            continue
        item = found[0].item
        account = session.get(Account, item.account_id)
        if account is None or not account.is_active:
            continue
        proposals[transaction.id] = BookingProposal(
            source="open_item",
            contra_account=account,
            partner_id=item.partner_id,
            open_item_id=item.id,
        )

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
            partner_id=rule.partner_id,
            rule_pattern=rule.pattern,
        )
    return proposals
