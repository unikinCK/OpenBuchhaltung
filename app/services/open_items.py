from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.sepa_export import SepaExportError, normalize_bic, normalize_iban
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    Account,
    BankTransaction,
    BusinessPartner,
    Company,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    OpenItemSettlement,
)

OPEN_ITEM_TYPES = {"receivable", "payable"}
# Sammelkontoseite und Rollenfeld des Partners je OPOS-Typ.
ITEM_TYPE_SUBLEDGER = {"receivable": SUBLEDGER_DEBTOR, "payable": SUBLEDGER_CREDITOR}


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
    counterparty_iban: str | None = None
    counterparty_bic: str | None = None
    partner_id: int | None = None


def list_open_items(
    *,
    session: Session,
    company_id: int,
    include_settled: bool = False,
    partner_id: int | None = None,
) -> list[OpenItem]:
    stmt = (
        select(OpenItem)
        .options(
            selectinload(OpenItem.account),
            selectinload(OpenItem.journal_entry),
            selectinload(OpenItem.partner),
        )
        .where(OpenItem.company_id == company_id)
        .order_by(OpenItem.status, OpenItem.due_date, OpenItem.entry_date, OpenItem.id)
    )
    if not include_settled:
        stmt = stmt.where(OpenItem.status == "open")
    if partner_id is not None:
        stmt = stmt.where(OpenItem.partner_id == partner_id)
    return session.execute(stmt).scalars().all()


def open_item_partner_number(item: OpenItem) -> str | None:
    """Personenkontonummer des Partners passend zur Seite des Postens."""
    partner = item.partner
    if partner is None:
        return None
    return partner.debtor_number if item.item_type == "receivable" else partner.creditor_number


def open_item_matches_query(item: OpenItem, query: str) -> bool:
    """Suche in Referenz, Gegenseite sowie Name und Nummern des Partners."""
    needle = query.lower()
    partner = item.partner
    haystacks = [item.reference, item.counterparty]
    if partner is not None:
        haystacks += [partner.name, partner.debtor_number, partner.creditor_number]
    return any(needle in (value or "").lower() for value in haystacks)


def is_overdue(item: OpenItem, today: date | None = None) -> bool:
    """Offen und die Fälligkeit liegt vor heute (Dashboard-Kachel, Filter „überfällig“)."""
    today = today or date.today()
    return item.status == "open" and item.due_date is not None and item.due_date < today


def _load_item_partner(
    *,
    session: Session,
    company_id: int,
    partner_id: int,
    item_type: str,
    account: Account,
) -> BusinessPartner:
    """Partner eines Postens: gleiche Gesellschaft, aktiv, Rolle und Sammelkonto passen."""
    partner = session.get(BusinessPartner, partner_id)
    if partner is None or partner.company_id != company_id:
        raise OpenItemError("Geschäftspartner nicht gefunden.")
    if not partner.is_active:
        raise OpenItemError(f"{partner.name} ist inaktiv und darf nicht neu zugeordnet werden.")
    subledger = ITEM_TYPE_SUBLEDGER[item_type]
    if subledger == SUBLEDGER_DEBTOR and partner.debtor_number is None:
        raise OpenItemError(f"{partner.name} ist kein Kunde; der Posten ist debitorisch.")
    if subledger == SUBLEDGER_CREDITOR and partner.creditor_number is None:
        raise OpenItemError(f"{partner.name} ist kein Lieferant; der Posten ist kreditorisch.")
    if account.subledger != subledger:
        side = "Debitoren" if subledger == SUBLEDGER_DEBTOR else "Kreditoren"
        raise OpenItemError(
            f"Konto {account.code} ist kein {side}-Sammelkonto. Das Kennzeichen wird in der "
            "Kontenpflege gesetzt."
        )
    return partner


def create_open_item(
    *, session: Session, payload: OpenItemInput, commit: bool = True
) -> OpenItem:
    """Legt einen Posten an; mit ``commit=False`` committet der Aufrufer (Ausgangsrechnung)."""
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

    try:
        counterparty_iban = normalize_iban(payload.counterparty_iban)
        counterparty_bic = normalize_bic(payload.counterparty_bic)
    except SepaExportError as exc:
        raise OpenItemError(str(exc)) from exc

    counterparty = (payload.counterparty or "").strip() or None
    due_date = payload.due_date
    partner = None
    if payload.partner_id is not None:
        partner = _load_item_partner(
            session=session,
            company_id=company.id,
            partner_id=payload.partner_id,
            item_type=item_type,
            account=account,
        )
        # Vorbelegung als Snapshot: was erfasst wurde, geht vor.
        counterparty = counterparty or partner.name
        if counterparty_iban is None:
            counterparty_iban = partner.iban
            counterparty_bic = counterparty_bic or partner.bic
        if due_date is None and partner.payment_term_days is not None:
            due_date = payload.entry_date + timedelta(days=partner.payment_term_days)

    item = OpenItem(
        tenant_id=company.tenant_id,
        company_id=company.id,
        account_id=account.id,
        journal_entry_id=linked_entry.id if linked_entry else None,
        partner_id=partner.id if partner else None,
        item_type=item_type,
        reference=reference,
        counterparty=counterparty,
        counterparty_iban=counterparty_iban,
        counterparty_bic=counterparty_bic,
        entry_date=payload.entry_date,
        due_date=due_date,
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
            "partner_id": item.partner_id,
            "original_amount": str(item.original_amount),
        },
    )
    if commit:
        session.commit()
        session.refresh(item)
    return item


def assign_partner_to_item(
    *, session: Session, item: OpenItem, partner_id: int | None, changed_by: str
) -> bool:
    """Setzt den Partner eines Postens ohne Commit; liefert, ob sich etwas geändert hat."""
    before = item.partner_id
    if partner_id == before:
        return False
    if partner_id is not None:
        partner = _load_item_partner(
            session=session,
            company_id=item.company_id,
            partner_id=partner_id,
            item_type=item.item_type,
            account=item.account,
        )
        item.counterparty = item.counterparty or partner.name
        if item.counterparty_iban is None:
            item.counterparty_iban = partner.iban
            item.counterparty_bic = item.counterparty_bic or partner.bic
    item.partner_id = partner_id
    log_audit_event(
        session=session,
        tenant_id=item.tenant_id,
        company_id=item.company_id,
        entity_type="open_item",
        entity_id=str(item.id),
        action="partner_assigned",
        changed_by=changed_by,
        payload={"reference": item.reference, "before": before, "after": partner_id},
    )
    return True


def set_open_item_partner(
    *, session: Session, open_item_id: int, partner_id: int | None, changed_by: str
) -> OpenItem:
    """Ordnet einem Posten einen Partner zu oder entfernt ihn (auch nach dem Ausgleich).

    Name, Bankdaten und Fälligkeit bleiben unverändert; fehlende Angaben werden aus dem
    Partner ergänzt. Der Freitext bleibt erhalten, er ist der Stand zum Zeitpunkt der Erfassung.
    """
    item = session.get(OpenItem, open_item_id)
    if item is None:
        raise OpenItemError("Offener Posten nicht gefunden.")
    if assign_partner_to_item(
        session=session, item=item, partner_id=partner_id, changed_by=changed_by
    ):
        session.commit()
        session.refresh(item)
    return item


def close_open_item_by_cancellation(
    *, session: Session, item: OpenItem, reversal_entry: JournalEntry, changed_by: str
) -> None:
    """Schließt den Posten einer stornierten Rechnung, ohne Commit.

    Kein Ausgleich: Es floss kein Geld, deshalb entsteht keine Zeile in ``OpenItemSettlement``.
    Bei Istversteuerung gälte sie sonst als Zahlung und löste Umsatzsteuer aus.
    """
    item.open_amount = Decimal("0.00")
    item.status = "settled"
    item.settled_at = datetime.now(timezone.utc)
    item.settled_by = changed_by
    log_audit_event(
        session=session,
        tenant_id=item.tenant_id,
        company_id=item.company_id,
        entity_type="open_item",
        entity_id=str(item.id),
        action="cancelled",
        changed_by=changed_by,
        payload={"reference": item.reference, "reversal_entry_id": reversal_entry.id},
    )
    session.flush()


def settle_open_item(
    *,
    session: Session,
    open_item_id: int,
    changed_by: str,
    amount: Decimal | None = None,
    bank_transaction_id: int | None = None,
    journal_entry_id: int | None = None,
    settlement_date: date | None = None,
) -> OpenItem:
    """Gleicht einen Posten aus. Das Zahlungsdatum ist ``settlement_date``, sonst das Datum der
    Buchung, des Bankumsatzes oder heute; es bestimmt bei Istversteuerung den Meldezeitraum."""
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

    _check_settlement_partner(
        session=session, item=item, bank_transaction=bank_transaction, linked_entry=linked_entry
    )

    settlement_amount = _settlement_amount(
        explicit_amount=amount,
        bank_transaction=bank_transaction,
        open_amount=item.open_amount,
    )
    if settlement_amount > item.open_amount:
        raise OpenItemError("Ausgleichsbetrag darf den offenen Betrag nicht übersteigen.")

    if settlement_date is None:
        if linked_entry is not None:
            settlement_date = linked_entry.entry_date
        elif bank_transaction is not None:
            settlement_date = bank_transaction.booking_date
        else:
            settlement_date = date.today()

    item.open_amount = (item.open_amount - settlement_amount).quantize(Decimal("0.01"))
    session.add(
        OpenItemSettlement(
            tenant_id=item.tenant_id,
            company_id=item.company_id,
            open_item_id=item.id,
            amount=settlement_amount,
            settlement_date=settlement_date,
            journal_entry_id=linked_entry.id if linked_entry else None,
            bank_transaction_id=bank_transaction.id if bank_transaction else None,
            created_by=changed_by,
        )
    )
    if linked_entry is not None:
        # Wird die Ausgleichsbuchung storniert, lebt der Posten um diesen
        # Betrag wieder auf (siehe reverse_journal_entry). Es wird nur der
        # letzte Ausgleich mit Buchung gemerkt.
        item.settlement_journal_entry_id = linked_entry.id
        item.settlement_amount = settlement_amount
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
            "settlement_date": settlement_date.isoformat(),
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


def _check_settlement_partner(
    *,
    session: Session,
    item: OpenItem,
    bank_transaction: BankTransaction | None,
    linked_entry: JournalEntry | None,
) -> None:
    """Weist einen Ausgleich ab, dessen Buchung einen anderen Partner bebucht.

    Geprüft werden nur Zeilen auf dem Sammelkonto des Postens; Zeilen ohne Partner oder ein
    Posten ohne Partner bleiben zulässig (Altbestand, Sammelzahlungen).
    """
    if item.partner_id is None:
        return
    entry_id = linked_entry.id if linked_entry is not None else None
    if entry_id is None and bank_transaction is not None:
        entry_id = bank_transaction.journal_entry_id
    if entry_id is None:
        return
    partner_ids = set(
        session.execute(
            select(JournalEntryLine.partner_id).where(
                JournalEntryLine.journal_entry_id == entry_id,
                JournalEntryLine.account_id == item.account_id,
                JournalEntryLine.partner_id.is_not(None),
            )
        ).scalars()
    )
    if partner_ids and item.partner_id not in partner_ids:
        raise OpenItemError(
            "Die Zahlung ist auf einen anderen Geschäftspartner gebucht als der Posten."
        )


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
