from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from domain.models import Account, BankTransaction, Company, JournalEntry, OpenItem

OPEN_ITEM_TYPES = {"receivable", "payable"}


class OpenItemError(ValueError):
    """Raised when an open item action is invalid."""


@dataclass(slots=True)
class OpenItemInput:
    company_id: int
    account_id: int
    item_type: str
    reference: str
    entry_date: date
    amount: Decimal
    changed_by: str
    journal_entry_id: int | None = None
    counterparty: str | None = None
    due_date: date | None = None


def list_open_items(
    *, session: Session, company_id: int, include_settled: bool = False
) -> list[OpenItem]:
    stmt = (
        select(OpenItem)
        .options(selectinload(OpenItem.account), selectinload(OpenItem.journal_entry))
        .where(OpenItem.company_id == company_id)
        .order_by(OpenItem.status, OpenItem.due_date, OpenItem.entry_date, OpenItem.id)
    )
    if not include_settled:
        stmt = stmt.where(OpenItem.status == "open")
    return session.execute(stmt).scalars().all()


def create_open_item(*, session: Session, payload: OpenItemInput) -> OpenItem:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise OpenItemError("Gesellschaft nicht gefunden.")

    account = session.get(Account, payload.account_id)
    if account is None or account.company_id != company.id:
        raise OpenItemError("OPOS-Konto nicht gefunden.")

    item_type = payload.item_type.strip()
    if item_type not in OPEN_ITEM_TYPES:
        raise OpenItemError("OPOS-Typ muss receivable oder payable sein.")

    reference = payload.reference.strip()
    if not reference:
        raise OpenItemError("Referenz ist Pflicht.")

    amount = _positive_amount(payload.amount)

    linked_entry = None
    if payload.journal_entry_id is not None:
        linked_entry = session.get(JournalEntry, payload.journal_entry_id)
        if linked_entry is None or linked_entry.company_id != company.id:
            raise OpenItemError("Verknüpfte Buchung nicht gefunden.")

    item = OpenItem(
        tenant_id=company.tenant_id,
        company_id=company.id,
        account_id=account.id,
        journal_entry_id=linked_entry.id if linked_entry else None,
        item_type=item_type,
        reference=reference,
        counterparty=(payload.counterparty or "").strip() or None,
        entry_date=payload.entry_date,
        due_date=payload.due_date,
        original_amount=amount,
        open_amount=amount,
        currency_code=company.currency_code,
        status="open",
    )
    session.add(item)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="open_item",
        entity_id=str(item.id),
        action="created",
        changed_by=payload.changed_by,
        payload={
            "reference": item.reference,
            "item_type": item.item_type,
            "account_id": item.account_id,
            "journal_entry_id": item.journal_entry_id,
            "original_amount": str(item.original_amount),
        },
    )
    session.commit()
    session.refresh(item)
    return item


def settle_open_item(
    *,
    session: Session,
    open_item_id: int,
    changed_by: str,
    amount: Decimal | None = None,
    bank_transaction_id: int | None = None,
    journal_entry_id: int | None = None,
) -> OpenItem:
    item = session.get(OpenItem, open_item_id)
    if item is None:
        raise OpenItemError("Offener Posten nicht gefunden.")
    if item.status != "open":
        raise OpenItemError("Der offene Posten ist bereits ausgeglichen.")

    bank_transaction = _load_bank_transaction(
        session=session, item=item, bank_transaction_id=bank_transaction_id
    )
    linked_entry = _load_journal_entry(
        session=session, item=item, journal_entry_id=journal_entry_id
    )

    settlement_amount = _settlement_amount(
        explicit_amount=amount,
        bank_transaction=bank_transaction,
        open_amount=item.open_amount,
    )
    if settlement_amount > item.open_amount:
        raise OpenItemError("Ausgleichsbetrag darf den offenen Betrag nicht übersteigen.")

    item.open_amount = (item.open_amount - settlement_amount).quantize(Decimal("0.01"))
    if bank_transaction is not None:
        item.bank_transaction_id = bank_transaction.id
        if bank_transaction.status == "open":
            bank_transaction.status = "matched"
        if linked_entry is not None:
            bank_transaction.journal_entry_id = linked_entry.id

    if item.open_amount == Decimal("0.00"):
        item.status = "settled"
        item.settled_at = datetime.now(timezone.utc)
        item.settled_by = changed_by

    action = "settled" if item.status == "settled" else "partially_settled"
    log_audit_event(
        session=session,
        tenant_id=item.tenant_id,
        company_id=item.company_id,
        entity_type="open_item",
        entity_id=str(item.id),
        action=action,
        changed_by=changed_by,
        payload={
            "settlement_amount": str(settlement_amount),
            "open_amount": str(item.open_amount),
            "bank_transaction_id": bank_transaction.id if bank_transaction else None,
            "journal_entry_id": linked_entry.id if linked_entry else None,
        },
    )
    session.commit()
    session.refresh(item)
    return item


def _load_bank_transaction(
    *, session: Session, item: OpenItem, bank_transaction_id: int | None
) -> BankTransaction | None:
    if bank_transaction_id is None:
        return None

    transaction = session.get(BankTransaction, bank_transaction_id)
    if transaction is None or transaction.company_id != item.company_id:
        raise OpenItemError("Bankumsatz nicht gefunden.")
    if transaction.status not in {"open", "matched", "booked"}:
        raise OpenItemError("Bankumsatz hat keinen ausgleichbaren Status.")

    if item.item_type == "receivable" and transaction.amount < 0:
        raise OpenItemError("Debitorische Posten werden mit Zahlungseingängen ausgeglichen.")
    if item.item_type == "payable" and transaction.amount > 0:
        raise OpenItemError("Kreditorische Posten werden mit Zahlungsausgängen ausgeglichen.")
    return transaction


def _load_journal_entry(
    *, session: Session, item: OpenItem, journal_entry_id: int | None
) -> JournalEntry | None:
    if journal_entry_id is None:
        return None

    entry = session.get(JournalEntry, journal_entry_id)
    if entry is None or entry.company_id != item.company_id:
        raise OpenItemError("Ausgleichsbuchung nicht gefunden.")
    return entry


def _settlement_amount(
    *,
    explicit_amount: Decimal | None,
    bank_transaction: BankTransaction | None,
    open_amount: Decimal,
) -> Decimal:
    if explicit_amount is not None:
        return _positive_amount(explicit_amount)
    if bank_transaction is not None:
        return min(abs(bank_transaction.amount), open_amount).quantize(Decimal("0.01"))
    return open_amount


def _positive_amount(amount: Decimal) -> Decimal:
    normalized = amount.quantize(Decimal("0.01"))
    if normalized <= Decimal("0.00"):
        raise OpenItemError("Betrag muss größer 0 sein.")
    return normalized
