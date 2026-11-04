from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
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
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
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

    Bei einem Posten in Fremdwährung ist ``foreign_amount`` der gezahlte Fremdbetrag (sonst der
    Bankumsatz oder der ganze offene Betrag) und ``amount`` der gezahlte Betrag in Firmenwährung
    (sonst aus ``exchange_rate``, der Ausgleichsbuchung oder dem Tageskurs). Die Differenz zum
    Buchwert ist der Kursgewinn oder -verlust; mit einer Ausgleichsbuchung wird sie gebucht.
    """
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

    if settlement_date is None:
        if linked_entry is not None:
            settlement_date = linked_entry.entry_date
        elif bank_transaction is not None:
            settlement_date = bank_transaction.booking_date
        else:
            settlement_date = date.today()

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
            linked_entry=linked_entry,
            settlement_date=settlement_date,
        )
        fx_difference = paid_amount - settlement_amount
        payment_entry_id = (linked_entry.id if linked_entry else None) or (
            bank_transaction.journal_entry_id if bank_transaction else None
        )
        if fx_difference != 0 and payment_entry_id is not None:
            fx_entry = _book_fx_difference(
                session=session,
                item=item,
                difference=fx_difference,
                entry_date=settlement_date,
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
        settlement_amount = _settlement_amount(
            explicit_amount=amount,
            bank_transaction=bank_transaction,
            open_amount=item.open_amount,
        )
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
            journal_entry_id=linked_entry.id if linked_entry else None,
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


def _paid_on_item_account(session: Session, item: OpenItem, entry_id: int) -> Decimal:
    """Betrag der Zahlungsbuchung auf dem Sammelkonto des Postens, in Firmenwährung."""
    column = (
        JournalEntryLine.credit_amount
        if item.item_type == "receivable"
        else JournalEntryLine.debit_amount
    )
    conditions = [
        JournalEntryLine.journal_entry_id == entry_id,
        JournalEntryLine.account_id == item.account_id,
    ]
    if item.partner_id is not None:
        conditions.append(JournalEntryLine.partner_id == item.partner_id)
    return sum(session.execute(select(column).where(*conditions)).scalars(), Decimal("0.00"))


def _foreign_settlement(
    *,
    session: Session,
    item: OpenItem,
    amount: Decimal | None,
    foreign_amount: Decimal | None,
    exchange_rate: Decimal | None,
    bank_transaction: BankTransaction | None,
    linked_entry: JournalEntry | None,
    settlement_date: date,
) -> tuple[Decimal, Decimal, Decimal]:
    """Fremdbetrag, ausgebuchter Buchwert und gezahlter Betrag in Firmenwährung."""
    if bank_transaction is not None and bank_transaction.currency_code:
        if bank_transaction.currency_code not in {item.foreign_currency_code, item.currency_code}:
            raise OpenItemError(
                f"Der Bankumsatz läuft in {bank_transaction.currency_code}, der Posten in "
                f"{item.foreign_currency_code}."
            )
    settled = foreign_amount
    if (
        settled is None
        and bank_transaction is not None
        and bank_transaction.currency_code == item.foreign_currency_code
    ):
        settled = min(abs(bank_transaction.amount), item.foreign_open_amount)
    if settled is None:
        settled = item.foreign_open_amount
    settled = settled.quantize(Decimal("0.01"))
    if settled <= 0:
        raise OpenItemError("Fremdbetrag muss größer 0 sein.")
    if settled > item.foreign_open_amount:
        raise OpenItemError(
            f"Der Fremdbetrag übersteigt den offenen Betrag von {item.foreign_open_amount} "
            f"{item.foreign_currency_code}."
        )

    # Der Rest geht vollständig heraus, damit kein Rundungscent auf dem Posten stehen bleibt.
    if settled == item.foreign_open_amount:
        carrying = item.open_amount
    else:
        carrying = min(
            (item.original_amount * settled / item.foreign_original_amount).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            item.open_amount,
        )

    if amount is not None:
        paid = _positive_amount(amount)
    elif exchange_rate is not None:
        if exchange_rate <= 0:
            raise OpenItemError("Der Kurs muss größer 0 sein.")
        paid = to_company_amount(settled, exchange_rate)
    else:
        entry_id = (linked_entry.id if linked_entry else None) or (
            bank_transaction.journal_entry_id if bank_transaction else None
        )
        paid = (
            _paid_on_item_account(session, item, entry_id)
            if entry_id is not None
            else Decimal("0.00")
        )
        if paid <= 0:
            try:
                paid = to_company_amount(
                    settled,
                    rate_for(
                        session=session,
                        tenant_id=item.tenant_id,
                        currency_code=item.foreign_currency_code,
                        on=settlement_date,
                    ),
                )
            except ExchangeRateError as exc:
                raise OpenItemError(str(exc)) from exc
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
