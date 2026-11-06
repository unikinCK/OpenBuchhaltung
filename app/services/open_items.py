from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.exchange_rates import (
    ExchangeRateError,
    fx_account,
    normalize_currency,
    rate_for,
    require_base_currency,
    to_company_amount,
)
from app.services.journal_entries import (
    SOURCE_FX_DIFFERENCE,
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    first_open_posting_date,
)
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
from domain.services.journal_entry_validation import JournalEntryValidationError

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
    # Fremdwährung (ADR-010). ``amount`` ist dann der Buchwert in Firmenwährung; 0 oder leer
    # rechnet ihn aus dem Fremdbetrag zum Kurs (Kurs ohne Angabe: Tageskurs des Belegdatums).
    foreign_currency_code: str | None = None
    foreign_amount: Decimal | None = None
    exchange_rate: Decimal | None = None


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


def _foreign_values(
    *, session: Session, company: Company, payload: OpenItemInput
) -> tuple[str | None, Decimal | None, Decimal | None, Decimal]:
    """Fremdwährung, Fremdbetrag, Kurs und Buchwert eines neuen Postens."""
    if not payload.foreign_currency_code and payload.foreign_amount is None:
        return None, None, None, payload.amount
    try:
        require_base_currency(company)
        code = normalize_currency(payload.foreign_currency_code)
        if code == company.currency_code:
            raise OpenItemError(f"{code} ist die Firmenwährung, nicht eine Fremdwährung.")
        if payload.foreign_amount is None or payload.foreign_amount <= 0:
            raise OpenItemError("Fremdbetrag muss größer 0 sein.")
        foreign_amount = payload.foreign_amount.quantize(Decimal("0.01"))
        rate = payload.exchange_rate
        if rate is None:
            rate = rate_for(
                session=session,
                tenant_id=company.tenant_id,
                currency_code=code,
                on=payload.entry_date,
            )
    except ExchangeRateError as exc:
        raise OpenItemError(str(exc)) from exc
    if rate <= 0:
        raise OpenItemError("Der Kurs muss größer 0 sein.")
    amount = payload.amount if payload.amount else to_company_amount(foreign_amount, rate)
    return code, foreign_amount, rate, amount


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

    foreign_code, foreign_amount, exchange_rate, amount = _foreign_values(
        session=session, company=company, payload=payload
    )
    amount = _positive_amount(amount)

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
        foreign_currency_code=foreign_code,
        foreign_original_amount=foreign_amount,
        foreign_open_amount=foreign_amount,
        exchange_rate=exchange_rate,
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
            "foreign_currency_code": item.foreign_currency_code,
            "foreign_amount": str(foreign_amount) if foreign_amount is not None else None,
            "exchange_rate": str(exchange_rate) if exchange_rate is not None else None,
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
    item.settlement_journal_entry_id = reversal_entry.id
    item.settlement_amount = item.open_amount
    item.open_amount = Decimal("0.00")
    if item.foreign_currency_code:
        item.foreign_open_amount = Decimal("0.00")
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
    foreign_amount: Decimal | None = None,
    exchange_rate: Decimal | None = None,
) -> OpenItem:
    """Gleicht einen Posten aus. Das Zahlungsdatum ist ``settlement_date``, sonst das Datum der
    Buchung, des Bankumsatzes oder heute; es bestimmt bei Istversteuerung den Meldezeitraum.
    Wird dieselbe Zahlung nach einem Storno neu zugeordnet, liegt es nie vor dem Storno: Sonst
    stünde sie rückwirkend doppelt in einem schon gemeldeten Zeitraum und am Bewertungsstichtag.

    Bei einem Posten in Fremdwährung ist ``foreign_amount`` der gezahlte Fremdbetrag und ``amount``
    der gezahlte Betrag in Firmenwährung; ableiten lässt sich beides nur aus einer Zahlung in der
    Währung des Postens (siehe ``_foreign_settlement``). Die Differenz zum Buchwert ist der
    Kursgewinn oder -verlust; gebucht wird sie, wenn die Zahlung auf dem Sammelkonto steht, im
    Zeitraum des Zahlungsdatums oder im nächsten offenen.

    Ein Bankumsatz oder eine Zahlungsbuchung darf mehrere Posten bezahlen, zusammen aber nur bis zu
    ihrem Betrag; was stornierte Ausgleiche belegt hatten, ist wieder frei. Dieselbe Zahlung zählt
    dabei nur einmal, ob sie über den Umsatz oder über dessen Buchung zugeordnet wird.
    """
    # Erst die Zahlung sperren, dann den Posten: den Umsatz, danach die Buchungen nach ihrer ID,
    # in derselben Reihenfolge wie der Storno (reverse_journal_entry). Zwei Ausgleiche oder ein
    # Ausgleich und ein Storno derselben Zahlung laufen so nacheinander, der zweite sieht, was der
    # erste getan hat. Die Buchung ohne Schlüsselsperre (FOR NO KEY UPDATE): Neue Verweise auf
    # sie, etwa ein Posten zur Rechnung, warten dann nicht.
    entry_ids = {journal_entry_id} - {None}
    if bank_transaction_id is not None:
        booked_entry_id = session.execute(
            select(BankTransaction.journal_entry_id)
            .where(BankTransaction.id == bank_transaction_id)
            .with_for_update()
        ).scalar()
        if booked_entry_id is not None:
            entry_ids.add(booked_entry_id)
    if entry_ids:
        session.execute(
            select(JournalEntry.id)
            .where(JournalEntry.id.in_(entry_ids))
            .order_by(JournalEntry.id)
            .with_for_update(key_share=True)
        )
    item = session.execute(
        select(OpenItem)
        .where(OpenItem.id == open_item_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
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

    if settlement_date is None:
        if linked_entry is not None:
            settlement_date = linked_entry.entry_date
        elif bank_transaction is not None:
            settlement_date = bank_transaction.booking_date
        else:
            settlement_date = date.today()
    # Dieselbe Zahlung: derselbe Umsatz, auch auf einem anderen Posten, oder dasselbe Datum auf
    # diesem Posten. Eine andere Zahlung behält ihr Datum.
    same_payment = [
        and_(
            OpenItemSettlement.open_item_id == item.id,
            OpenItemSettlement.settlement_date == settlement_date,
        )
    ]
    if bank_transaction is not None:
        same_payment.append(OpenItemSettlement.bank_transaction_id == bank_transaction.id)
    last_reversal = session.execute(
        select(func.max(OpenItemSettlement.reversed_on)).where(or_(*same_payment))
    ).scalar()
    if last_reversal is not None and settlement_date < last_reversal:
        settlement_date = last_reversal

    payment_entry_id = (linked_entry.id if linked_entry else None) or (
        bank_transaction.journal_entry_id if bank_transaction else None
    )
    if payment_entry_id is not None:
        _refuse_reversed_payment(session, session.get(JournalEntry, payment_entry_id))
    if item.foreign_currency_code and bank_transaction is not None and payment_entry_id is None:
        raise OpenItemError(
            "Der Bankumsatz ist noch nicht gebucht. Bitte ihn zuerst buchen, sonst steht die "
            "Zahlung nicht im Hauptbuch und die Kursdifferenz bleibt ungebucht."
        )
    settled_foreign = paid_amount = fx_difference = None
    fx_entry = None
    if item.foreign_currency_code:
        settled_foreign, settlement_amount, paid_amount = _foreign_settlement(
            session=session,
            item=item,
            amount=amount,
            foreign_amount=foreign_amount,
            exchange_rate=exchange_rate,
            bank_transaction=bank_transaction,
            payment_entry_id=payment_entry_id,
            settlement_date=settlement_date,
        )
        fx_difference = paid_amount - settlement_amount
        # Nur was die Zahlung auf das Sammelkonto gebracht hat, lässt dort eine Differenz stehen.
        if _payment_reaches_item_account(
            session=session, item=item, entry_id=payment_entry_id, paid=paid_amount
        ) and fx_difference != 0:
            fx_entry = _book_fx_difference(
                session=session,
                item=item,
                difference=fx_difference,
                entry_date=first_open_posting_date(
                    session=session, company_id=item.company_id, on=settlement_date
                ),
                changed_by=changed_by,
            )
        item.foreign_open_amount = (item.foreign_open_amount - settled_foreign).quantize(
            Decimal("0.01")
        )
    else:
        if (
            bank_transaction is not None
            and bank_transaction.currency_code
            and bank_transaction.currency_code != item.currency_code
        ):
            raise OpenItemError(
                f"Der Bankumsatz läuft in {bank_transaction.currency_code}, der Posten in "
                f"{item.currency_code}."
            )
        limit = _payment_limit(
            session,
            item=item,
            bank_transaction=bank_transaction,
            payment_entry_id=payment_entry_id,
        )
        settlement_amount = _settlement_amount(
            explicit_amount=amount,
            payment_left=limit.left if limit is not None else None,
            open_amount=item.open_amount,
        )
        if limit is not None and settlement_amount > limit.left:
            raise OpenItemError(limit.exceeded)
    if settlement_amount > item.open_amount:
        raise OpenItemError("Ausgleichsbetrag darf den offenen Betrag nicht übersteigen.")

    item.open_amount = (item.open_amount - settlement_amount).quantize(Decimal("0.01"))
    session.add(
        OpenItemSettlement(
            tenant_id=item.tenant_id,
            company_id=item.company_id,
            open_item_id=item.id,
            amount=settlement_amount,
            settlement_date=settlement_date,
            # Auch die Buchung des Bankumsatzes: Ihr Storno nimmt den Ausgleich zurück.
            journal_entry_id=payment_entry_id,
            bank_transaction_id=bank_transaction.id if bank_transaction else None,
            created_by=changed_by,
            foreign_amount=settled_foreign,
            paid_amount=paid_amount,
            fx_difference=fx_difference,
            fx_journal_entry_id=fx_entry.id if fx_entry is not None else None,
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

    settled_completely = (
        item.foreign_open_amount == Decimal("0.00")
        if item.foreign_currency_code
        else item.open_amount == Decimal("0.00")
    )
    if settled_completely:
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
            "foreign_amount": str(settled_foreign) if settled_foreign is not None else None,
            "paid_amount": str(paid_amount) if paid_amount is not None else None,
            "fx_difference": str(fx_difference) if fx_difference is not None else None,
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


def _refuse_reversed_payment(session: Session, entry: JournalEntry) -> None:
    """Eine stornierte Buchung oder ein Storno ist keine Zahlung."""
    if entry.reversal_of_id is not None:
        raise OpenItemError(f"Buchung {entry.posting_number} ist eine Stornobuchung.")
    reversal = session.execute(
        select(JournalEntry.posting_number).where(JournalEntry.reversal_of_id == entry.id)
    ).first()
    if reversal is not None:
        raise OpenItemError(
            f"Buchung {entry.posting_number} ist storniert ({reversal.posting_number})."
        )


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


def _collective_lines(session: Session, item: OpenItem, entry_id: int) -> list[JournalEntryLine]:
    """Zeilen der Zahlungsbuchung auf dem Sammelkonto, die diesen Posten bezahlen können.

    Zeilen ohne Partner zählen mit, solche eines anderen Partners nicht (Sammelzahlung).
    """
    column = (
        JournalEntryLine.credit_amount
        if item.item_type == "receivable"
        else JournalEntryLine.debit_amount
    )
    conditions = [
        JournalEntryLine.journal_entry_id == entry_id,
        JournalEntryLine.account_id == item.account_id,
        column > 0,
    ]
    if item.partner_id is not None:
        conditions.append(
            or_(
                JournalEntryLine.partner_id == item.partner_id,
                JournalEntryLine.partner_id.is_(None),
            )
        )
    return list(session.execute(select(JournalEntryLine).where(*conditions)).scalars())


def _settlements_of_payment(
    session: Session, *, entry_id: int | None, bank_transaction_id: int | None
) -> list[OpenItemSettlement]:
    """Nicht stornierte Ausgleiche, die schon auf dieser Zahlung stehen."""
    payment = []
    if entry_id is not None:
        payment.append(OpenItemSettlement.journal_entry_id == entry_id)
    if bank_transaction_id is not None:
        payment.append(OpenItemSettlement.bank_transaction_id == bank_transaction_id)
    return _active_settlements(session, *payment)


def _active_settlements(session: Session, *payment) -> list[OpenItemSettlement]:
    """Nicht stornierte Ausgleiche, die eine der Bedingungen erfüllen."""
    if not payment:
        return []
    return list(
        session.execute(
            select(OpenItemSettlement).where(
                or_(*payment), OpenItemSettlement.reversed_on.is_(None)
            )
        ).scalars()
    )


def _paid_of(settlement: OpenItemSettlement) -> Decimal:
    """Was ein Ausgleich von der Zahlung belegt, in Firmenwährung."""
    return settlement.paid_amount if settlement.paid_amount is not None else settlement.amount


def _competes_for_lines(
    item: OpenItem, other: OpenItem, lines: list[JournalEntryLine]
) -> bool:
    """Belegt ``other`` dieselben Zeilen? Zeilen mit Partner gehören nur dessen Posten."""
    if other.account_id != item.account_id:
        return False
    if item.partner_id is None or other.partner_id in (item.partner_id, None):
        return True
    return not any(line.partner_id is not None for line in lines)


def _payment_reaches_item_account(
    *, session: Session, item: OpenItem, entry_id: int | None, paid: Decimal
) -> bool:
    """Steht die Zahlung auf dem Sammelkonto? Mehr als dort gebucht lässt sich nicht zuordnen."""
    if entry_id is None:
        return False
    usage = _entry_usage(
        session,
        item=item,
        entry_id=entry_id,
        rows=_settlements_of_payment(session, entry_id=entry_id, bank_transaction_id=None),
    )
    if usage is None:
        return False
    booked, assigned = usage
    if paid + assigned > booked:
        raise OpenItemError(
            f"Die Buchung steht nur mit {booked} {item.currency_code} auf dem Sammelkonto, davon "
            f"sind {assigned} {item.currency_code} schon zugeordnet. {paid} "
            f"{item.currency_code} passen nicht mehr hinein."
        )
    return True


def _entry_usage(
    session: Session, *, item: OpenItem, entry_id: int, rows: list[OpenItemSettlement]
) -> tuple[Decimal, Decimal] | None:
    """Was die Buchung auf dem Sammelkonto für den Posten bucht und was ``rows`` davon belegen.

    ``None``, wenn sie dort nichts bucht, womit sich der Posten bezahlen lässt.
    """
    lines = _collective_lines(session, item, entry_id)
    if not lines:
        return None
    booked = sum((line.debit_amount + line.credit_amount for line in lines), Decimal("0.00"))
    assigned = sum(
        (_paid_of(row) for row in rows if _competes_for_lines(item, row.open_item, lines)),
        Decimal("0.00"),
    )
    return booked, assigned


@dataclass(slots=True)
class _ForeignPayment:
    """Was eine Zahlung in der Währung des Postens nennt und was davon noch frei ist."""

    foreign: Decimal
    foreign_left: Decimal
    paid: Decimal | None = None
    paid_left: Decimal | None = None


def _left_of_payment(
    lines: list[JournalEntryLine],
    same_currency: list[OpenItemSettlement],
    others: list[OpenItemSettlement],
) -> _ForeignPayment:
    """Rest einer Zahlung. Ausgleiche anderer Währung belegen den Fremdbetrag anteilig."""
    foreign = sum((line.foreign_amount for line in lines), Decimal("0.00"))
    paid = sum((line.debit_amount + line.credit_amount for line in lines), Decimal("0.00"))
    paid_by_others = sum((_paid_of(row) for row in others), Decimal("0.00"))
    return _ForeignPayment(
        foreign=foreign,
        foreign_left=foreign
        - sum((row.foreign_amount for row in same_currency), Decimal("0.00"))
        - (foreign * paid_by_others / paid).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        paid=paid,
        paid_left=paid
        - paid_by_others
        - sum((_paid_of(row) for row in same_currency), Decimal("0.00")),
    )


def _foreign_payment_source(
    *,
    session: Session,
    item: OpenItem,
    entry_id: int | None,
    bank_transaction: BankTransaction | None,
) -> _ForeignPayment | None:
    """Was die Zahlung selbst in der Währung des Postens nennt.

    Zeilen auf dem Sammelkonto gehen vor: Sie gehören einer Währung und oft einem Partner, nur
    Ausgleiche derselben Art belegen sie. Auf der Geldseite kommt alles aus demselben Geld, dort
    zählen auch andere Posten der Buchung. Ein Bankumsatz nennt nur den Fremdbetrag.
    """
    code = item.foreign_currency_code
    earlier = _settlements_of_payment(
        session,
        entry_id=entry_id,
        bank_transaction_id=bank_transaction.id if bank_transaction else None,
    )
    same_currency = [
        row
        for row in earlier
        if row.foreign_amount is not None and row.open_item.foreign_currency_code == code
    ]
    if entry_id is not None:
        collective = [
            line
            for line in _collective_lines(session, item, entry_id)
            if line.foreign_currency_code == code
        ]
        if collective:
            rows = [
                row
                for row in same_currency
                if row.journal_entry_id == entry_id
                and _competes_for_lines(item, row.open_item, collective)
            ]
            return _left_of_payment(collective, rows, [])
        side = (
            JournalEntryLine.debit_amount
            if item.item_type == "receivable"
            else JournalEntryLine.credit_amount
        )
        money = list(
            session.execute(
                select(JournalEntryLine).where(
                    JournalEntryLine.journal_entry_id == entry_id,
                    JournalEntryLine.foreign_currency_code == code,
                    JournalEntryLine.account_id != item.account_id,
                    side > 0,
                )
            ).scalars()
        )
        if money:
            others = [row for row in earlier if row not in same_currency]
            return _left_of_payment(money, same_currency, others)
    if bank_transaction is not None and bank_transaction.currency_code == code:
        foreign = abs(bank_transaction.amount)
        used = sum((row.foreign_amount for row in same_currency), Decimal("0.00"))
        return _ForeignPayment(foreign=foreign, foreign_left=foreign - used)
    return None


def _foreign_settlement(
    *,
    session: Session,
    item: OpenItem,
    amount: Decimal | None,
    foreign_amount: Decimal | None,
    exchange_rate: Decimal | None,
    bank_transaction: BankTransaction | None,
    payment_entry_id: int | None,
    settlement_date: date,
) -> tuple[Decimal, Decimal, Decimal]:
    """Fremdbetrag, ausgebuchter Buchwert und gezahlter Betrag in Firmenwährung.

    Eine Zahlung nur in Euro sagt nicht, wie viel Fremdwährung sie deckt und ob sie mehrere Posten
    bezahlt. Eine Differenz wäre dann erfunden, deshalb müssen hier beide Beträge angegeben werden
    (oder der Fremdbetrag und der Kurs). Der ganze offene Betrag gilt nur, wenn keine Zahlung
    genannt ist. Eine Buchung mit Zeilen in der Währung des Postens liefert beides anteilig; was
    andere Posten schon belegen, zählt nicht mehr.
    """
    code = item.foreign_currency_code
    if bank_transaction is not None and bank_transaction.currency_code:
        if bank_transaction.currency_code not in {code, item.currency_code}:
            raise OpenItemError(
                f"Der Bankumsatz läuft in {bank_transaction.currency_code}, der Posten in {code}."
            )

    source = _foreign_payment_source(
        session=session, item=item, entry_id=payment_entry_id, bank_transaction=bank_transaction
    )
    if source is not None and source.foreign_left <= 0:
        raise OpenItemError(f"Die Zahlung über {source.foreign} {code} ist schon zugeordnet.")

    settled = foreign_amount
    if settled is None:
        if source is not None:
            settled = min(source.foreign_left, item.foreign_open_amount)
        elif amount is not None or payment_entry_id is not None:
            raise OpenItemError(
                f"Eine Zahlung in {item.currency_code} sagt nicht, wie viel {code} sie deckt: "
                f"Bitte den gezahlten Betrag in {code} angeben."
            )
        else:
            settled = item.foreign_open_amount
    settled = settled.quantize(Decimal("0.01"))
    if settled <= 0:
        raise OpenItemError("Fremdbetrag muss größer 0 sein.")
    if settled > item.foreign_open_amount:
        raise OpenItemError(
            f"Der Fremdbetrag übersteigt den offenen Betrag von {item.foreign_open_amount} "
            f"{code}."
        )
    if source is not None and settled > source.foreign_left:
        raise OpenItemError(
            f"Der Fremdbetrag übersteigt die Zahlung, von ihr sind noch {source.foreign_left} "
            f"{code} frei."
        )

    # Der Rest geht vollständig heraus, damit kein Rundungscent auf dem Posten stehen bleibt. Bei
    # einer Teilzahlung bleibt mindestens ein Cent für ihn: Ausgleiche über 0,00 gibt es nicht.
    if settled == item.foreign_open_amount:
        carrying = item.open_amount
    else:
        carrying = min(
            (item.original_amount * settled / item.foreign_original_amount).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            item.open_amount - Decimal("0.01"),
        )
        if carrying <= 0:
            raise OpenItemError(
                f"{settled} {code} sind weniger als ein Cent Buchwert. Bitte einen größeren "
                "Betrag oder den ganzen Rest ausgleichen."
            )

    if amount is not None:
        paid = _positive_amount(amount)
    elif exchange_rate is not None:
        if exchange_rate <= 0:
            raise OpenItemError("Der Kurs muss größer 0 sein.")
        paid = to_company_amount(settled, exchange_rate)
    elif source is not None and source.paid_left is not None:
        paid = source.paid_left
        if settled != source.foreign_left:
            share = source.paid * settled / source.foreign
            paid = min(share.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), source.paid_left)
    elif payment_entry_id is not None:
        raise OpenItemError(
            "Die Zahlung nennt keinen Betrag für diesen Posten: Bitte den auf ihn "
            f"gezahlten Betrag in {item.currency_code} oder den Kurs angeben."
        )
    else:
        try:
            rate = rate_for(
                session=session,
                tenant_id=item.tenant_id,
                currency_code=code,
                on=settlement_date,
            )
        except ExchangeRateError as exc:
            raise OpenItemError(str(exc)) from exc
        paid = to_company_amount(settled, rate)
    if paid <= 0:
        raise OpenItemError(
            f"Für {settled} {code} bleibt kein Betrag in {item.currency_code}. Bitte den gezahlten "
            "Betrag angeben."
        )
    return settled, carrying, paid


def _book_fx_difference(
    *,
    session: Session,
    item: OpenItem,
    difference: Decimal,
    entry_date: date,
    changed_by: str,
) -> JournalEntry:
    """Bucht die Kursdifferenz einer Zahlung: ``difference`` = gezahlt minus Buchwert.

    Die Zahlungsbuchung bewegt das Sammelkonto um den gezahlten Betrag, ausgebucht wird der
    Buchwert. Die Differenz steht danach noch auf dem Sammelkonto und geht hier in den Erfolg.
    """
    gain = difference > 0 if item.item_type == "receivable" else difference < 0
    value = abs(difference)
    try:
        result_account = fx_account(session, item.company_id, gain=gain)
    except ExchangeRateError as exc:
        raise OpenItemError(str(exc)) from exc
    zero = Decimal("0.00")
    collective_line = JournalLineInput(
        account_id=item.account_id,
        debit_amount=value if gain else zero,
        credit_amount=zero if gain else value,
        partner_id=item.partner_id,
        description=f"Kursdifferenz {item.reference}",
    )
    result_line = JournalLineInput(
        account_id=result_account.id,
        debit_amount=zero if gain else value,
        credit_amount=value if gain else zero,
        description=f"Kursdifferenz {item.reference}",
    )
    try:
        return create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=item.company_id,
                entry_date=entry_date,
                description=f"Kursdifferenz {item.reference} ({item.foreign_currency_code})",
                status="posted",
                changed_by=changed_by,
                lines=[collective_line, result_line],
                source=SOURCE_FX_DIFFERENCE,
                expand_tax_lines=False,
            ),
            commit=False,
        )
    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        raise OpenItemError(f"Kursdifferenz nicht gebucht: {exc}") from exc


@dataclass(slots=True)
class _PaymentLimit:
    """Was von einer Zahlung noch frei ist, und die Meldung für einen höheren Betrag."""

    left: Decimal
    exceeded: str


def _payment_limit(
    session: Session,
    *,
    item: OpenItem,
    bank_transaction: BankTransaction | None,
    payment_entry_id: int | None,
) -> _PaymentLimit | None:
    """Obergrenze eines Ausgleichs in Firmenwährung: der Rest von Umsatz und Zahlungsbuchung.

    Ohne Umsatz ist die Buchung die Zahlung und muss den Posten auf seinem Konto bezahlen. Mit
    Umsatz begrenzt sie nur, wenn sie das tut; sonst ist sie nur verknüpft (etwa die Rechnung).
    """
    limits = []
    if bank_transaction is not None:
        limits.append(
            _bank_transaction_limit(
                session,
                item=item,
                bank_transaction=bank_transaction,
                entry_ids={bank_transaction.journal_entry_id, payment_entry_id} - {None},
            )
        )
    if payment_entry_id is not None:
        limit = _entry_limit(
            session, item=item, entry_id=payment_entry_id, required=bank_transaction is None
        )
        if limit is not None:
            limits.append(limit)
    return min(limits, key=lambda limit: limit.left, default=None)


def _bank_transaction_limit(
    session: Session, *, item: OpenItem, bank_transaction: BankTransaction, entry_ids: set[int]
) -> _PaymentLimit:
    """Rest des Umsatzes: Betrag minus nicht stornierte Ausgleiche mit ihm oder nur über seine
    Buchung. Ausgleiche anderer Umsätze mit derselben verknüpften Buchung zählen nicht.

    Ausgleiche von Posten in Fremdwährung belegen ihren gezahlten Betrag in Firmenwährung.
    """
    payment = abs(bank_transaction.amount)
    rows = _active_settlements(
        session,
        OpenItemSettlement.bank_transaction_id == bank_transaction.id,
        and_(
            OpenItemSettlement.bank_transaction_id.is_(None),
            OpenItemSettlement.journal_entry_id.in_(entry_ids),
        ),
    )
    used = sum((_paid_of(row) for row in rows), Decimal("0.00"))
    left = (payment - used).quantize(Decimal("0.01"))
    if left <= 0:
        raise OpenItemError(
            f"Der Bankumsatz über {payment} {item.currency_code} ist schon vollständig "
            "offenen Posten zugeordnet."
        )
    return _PaymentLimit(
        left=left,
        exceeded=(
            f"Der Betrag übersteigt den Bankumsatz, von ihm sind noch {left} "
            f"{item.currency_code} frei."
        ),
    )


def _entry_limit(
    session: Session, *, item: OpenItem, entry_id: int, required: bool
) -> _PaymentLimit | None:
    """Rest der Buchung: ihre Zahlungszeilen auf dem Konto des Postens minus nicht stornierte
    Ausgleiche, die sie belegen (``_collective_lines``, ``_competes_for_lines``).

    Mit dabei sind Ausgleiche über einen Umsatz, dessen Buchung sie ist, auch ältere, die nur den
    Umsatz kennen. Bucht sie dort nichts, ist das ohne Umsatz ein Fehler (``required``).
    """
    entry = session.get(JournalEntry, entry_id)
    rows = _active_settlements(
        session,
        OpenItemSettlement.journal_entry_id == entry_id,
        and_(
            OpenItemSettlement.journal_entry_id.is_(None),
            OpenItemSettlement.bank_transaction_id.in_(
                select(BankTransaction.id).where(BankTransaction.journal_entry_id == entry_id)
            ),
        ),
    )
    usage = _entry_usage(session, item=item, entry_id=entry_id, rows=rows)
    if usage is None:
        if not required:
            return None
        side = "Haben" if item.item_type == "receivable" else "Soll"
        raise OpenItemError(
            f"Buchung {entry.posting_number} bucht auf Konto {item.account.code} nichts im "
            f"{side}, was diesen Posten bezahlt."
        )
    booked, assigned = usage
    left = (booked - assigned).quantize(Decimal("0.01"))
    if left <= 0:
        raise OpenItemError(
            f"Buchung {entry.posting_number} steht mit {booked} {item.currency_code} auf Konto "
            f"{item.account.code}, das ist schon vollständig offenen Posten zugeordnet."
        )
    return _PaymentLimit(
        left=left,
        exceeded=(
            f"Der Betrag übersteigt die Buchung {entry.posting_number}, von ihr sind noch "
            f"{left} {item.currency_code} frei."
        ),
    )


def _settlement_amount(
    *,
    explicit_amount: Decimal | None,
    payment_left: Decimal | None,
    open_amount: Decimal,
) -> Decimal:
    if explicit_amount is not None:
        return _positive_amount(explicit_amount)
    if payment_left is not None:
        return min(payment_left, open_amount)
    return open_amount


def _positive_amount(amount: Decimal) -> Decimal:
    normalized = amount.quantize(Decimal("0.01"))
    if normalized <= Decimal("0.00"):
        raise OpenItemError("Betrag muss größer 0 sein.")
    return normalized
