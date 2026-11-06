"""Kontierungsvorschlag für einen ausgelesenen Beleg (Partner, Konto, Steuercode).

Quellen in dieser Reihenfolge:

1. ``history`` – letzte Buchung desselben Geschäftspartners (siehe
   ``partner_recognition``) bzw. – ohne Stammsatz – die letzte Buchung, deren
   Buchungstext den Lieferantennamen enthält („wie zuletzt“). Übernommen wird das
   Aufwands- bzw. Anlagekonto dieser Buchung.
2. ``ki`` – das Konto, das das LLM beim Auslesen aus dem Kontenplan gewählt hat
   (:attr:`ReceiptExtraction.suggested_account_code`).

Der Steuercode folgt aus dem erkannten Steuersatz (Vorsteuer-Code mit gleichem
Satz); bei Reverse Charge (§ 13b UStG) und ohne Steuer bleibt er leer. Weist der
Beleg mehrere Steuersätze aus, enthält der Vorschlag die Aufteilung je Satz
(``tax_lines``) mit dem jeweiligen Vorsteuer-Code.

Der Partner des Vorschlags ist nur bei eindeutiger Erkennung gesetzt. Ein unsicherer
Treffer steht allein in ``partner_match`` und bleibt ein Vorschlag zum Bestätigen.

Grundlage für die Vorbelegung der Buchungsmasken (Beleg-OCR, Belegabgleich) und
das Feld ``booking_proposal`` der REST-/MCP-Antworten.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.incoming_invoice import input_tax_code_for_rate
from app.services.partner_identifiers import find_ibans, find_tax_numbers, find_vat_ids
from app.services.partner_recognition import (
    NO_MATCH,
    PartnerMatch,
    PartnerSignals,
    load_partner_directory,
    name_tokens,
    proposed_partner_data,
)
from app.services.partners import partner_display_number
from app.services.receipt_ocr import ReceiptExtraction
from domain.models import (
    SUBLEDGER_CREDITOR,
    Account,
    BusinessPartner,
    FixedAsset,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)


@dataclass(frozen=True, slots=True)
class ProposedTaxLine:
    """Ein Steuersatz der vorgeschlagenen Aufteilung (mehrere Steuersätze)."""

    rate: Decimal
    net_amount: Decimal
    tax_amount: Decimal
    tax_code: TaxCode | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "rate": str(self.rate),
            "net_amount": str(self.net_amount),
            "tax_amount": str(self.tax_amount),
            "tax_code_id": self.tax_code.id if self.tax_code else None,
            "tax_code": self.tax_code.code if self.tax_code else None,
        }


@dataclass(frozen=True, slots=True)
class ReceiptBookingProposal:
    """Vorschlag für Partner, Aufwands-/Anlagekonto und Steuercode eines Belegs."""

    source: str | None  # history | ki | None
    reason: str
    account: Account | None = None
    tax_code: TaxCode | None = None
    partner: BusinessPartner | None = None
    history_entry_id: int | None = None
    # Aufteilung je Steuersatz; leer bei einem einzigen Satz.
    tax_lines: tuple[ProposedTaxLine, ...] = ()
    partner_match: PartnerMatch = NO_MATCH
    # Bankverbindung des Belegs ohne eigene IBAN (Snapshot für den offenen Posten).
    supplier_iban: str | None = None
    proposed_partner: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        partner = self.partner
        return {
            "source": self.source,
            "reason": self.reason,
            "expense_account_id": self.account.id if self.account else None,
            "expense_account_code": self.account.code if self.account else None,
            "expense_account_name": self.account.name if self.account else None,
            "tax_code_id": self.tax_code.id if self.tax_code else None,
            "tax_code": self.tax_code.code if self.tax_code else None,
            "partner_id": partner.id if partner else None,
            "partner_number": (
                partner_display_number(partner, SUBLEDGER_CREDITOR) if partner else None
            ),
            "partner_name": partner.name if partner else None,
            "partner_match": self.partner_match.as_dict(),
            "supplier_iban": self.supplier_iban,
            "proposed_partner": self.proposed_partner,
            "history_entry_id": self.history_entry_id,
            "tax_lines": [line.as_dict() for line in self.tax_lines],
        }


def booking_accounts(session: Session, company_id: int) -> list[Account]:
    """Aktive Aufwandskonten plus Anlagekonten, auf denen Anlagegüter geführt werden.

    Abschreibungskonten zählen nicht: Belege werden nie direkt als AfA gebucht.
    """
    asset_rows = session.execute(
        select(FixedAsset.asset_account_id, FixedAsset.depreciation_account_id).where(
            FixedAsset.company_id == company_id
        )
    ).all()
    asset_account_ids = {row[0] for row in asset_rows}
    depreciation_account_ids = {row[1] for row in asset_rows}
    accounts = session.execute(
        select(Account)
        .where(Account.company_id == company_id, Account.is_active.is_(True))
        .order_by(Account.code)
    ).scalars()
    return [
        account
        for account in accounts
        if (account.account_type == "expense" and account.id not in depreciation_account_ids)
        or account.id in asset_account_ids
    ]


def account_choices(session: Session, company_id: int) -> list[tuple[str, str]]:
    """(Kontonummer, Bezeichnung) der Konten, aus denen das LLM wählen darf."""
    return [(account.code, account.name) for account in booking_accounts(session, company_id)]


def receipt_partner_signals(extraction: ReceiptExtraction) -> PartnerSignals:
    """Kennungen des Belegs: was die KI als Lieferant gelesen hat und was im Text steht.

    Partnernummern zählen hier nicht: „Kundennummer“ ist auf einer Eingangsrechnung unsere
    Nummer beim Lieferanten und hat mit unserem Nummernkreis nichts zu tun.
    """
    vat_ids = [extraction.supplier_vat_id] if extraction.supplier_vat_id else []
    return PartnerSignals(
        vat_ids=tuple(dict.fromkeys([*vat_ids, *find_vat_ids(extraction.raw_text)])),
        tax_numbers=tuple(find_tax_numbers(extraction.raw_text)),
        ibans=tuple(find_ibans(extraction.raw_text)),
        name=extraction.supplier,
    )


def _last_line(
    session: Session, company_id: int, account_ids: set[int], *conditions
) -> tuple[JournalEntryLine, JournalEntry] | None:
    if not account_ids:
        return None
    row = session.execute(
        select(JournalEntryLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.reversal_of_id.is_(None),
            JournalEntryLine.account_id.in_(account_ids),
            JournalEntryLine.debit_amount > 0,
            *conditions,
        )
        .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else None


def _history_line(
    session: Session,
    company_id: int,
    account_ids: set[int],
    *,
    partner: BusinessPartner | None,
    supplier: str | None,
) -> tuple[JournalEntryLine, JournalEntry] | None:
    if partner is not None:
        found = _last_line(
            session,
            company_id,
            account_ids,
            JournalEntry.id.in_(
                select(JournalEntryLine.journal_entry_id).where(
                    JournalEntryLine.partner_id == partner.id
                )
            ),
        )
        if found is not None:
            return found
    # Ohne Stammsatz: markantestes Namenswort im Buchungstext suchen.
    tokens = sorted(name_tokens(supplier), key=len, reverse=True)
    tokens = [token for token in tokens if len(token) >= 4 and not token.isdigit()]
    if not tokens:
        return None
    return _last_line(
        session,
        company_id,
        account_ids,
        func.lower(JournalEntry.description).contains(tokens[0]),
    )


def _tax_code_for(
    session: Session, company_id: int, extraction: ReceiptExtraction
) -> TaxCode | None:
    if extraction.reverse_charge or extraction.tax_rate is None:
        return None
    if not extraction.tax_amount or extraction.tax_rate <= 0:
        return None
    return input_tax_code_for_rate(session, company_id, extraction.tax_rate)


def propose_tax_lines(
    session: Session,
    company_id: int,
    tax_lines: list[tuple[Decimal, Decimal, Decimal]],
    *,
    reverse_charge: bool = False,
) -> tuple[ProposedTaxLine, ...]:
    """Aufteilung je Steuersatz mit Vorsteuer-Code – nur bei mehreren Sätzen.

    Zeilen mit gleichem Satz werden zusammengefasst, der größte Nettobetrag
    steht vorn. Steuerfreie Anteile (0 %) bleiben ohne Steuercode.
    """
    if reverse_charge:
        return ()
    by_rate: dict[Decimal, tuple[Decimal, Decimal]] = {}
    for rate, net, tax in tax_lines:
        total_net, total_tax = by_rate.get(rate, (Decimal("0.00"), Decimal("0.00")))
        by_rate[rate] = (total_net + net, total_tax + tax)
    if len(by_rate) < 2:
        return ()
    lines = [
        ProposedTaxLine(
            rate=rate,
            net_amount=net,
            tax_amount=tax,
            tax_code=(
                input_tax_code_for_rate(session, company_id, rate) if rate > 0 and tax else None
            ),
        )
        for rate, (net, tax) in by_rate.items()
    ]
    return tuple(sorted(lines, key=lambda line: -line.net_amount))


def propose_receipt_booking(
    session: Session, company_id: int, extraction: ReceiptExtraction
) -> ReceiptBookingProposal:
    """Kontierungsvorschlag für einen ausgelesenen Beleg."""
    accounts = {account.id: account for account in booking_accounts(session, company_id)}
    directory = load_partner_directory(session, company_id)
    signals = receipt_partner_signals(extraction)
    match = directory.match(signals, role=SUBLEDGER_CREDITOR)
    partner = match.partner
    document_iban = next(iter(directory.foreign_ibans(signals)), None)
    proposed_partner = (
        None
        if partner is not None
        else proposed_partner_data(
            name=extraction.supplier,
            vat_id=next(iter(directory.foreign_vat_ids(signals)), None),
            tax_number=next(iter(signals.tax_numbers), None),
            iban=document_iban,
        )
    )
    # Für den offenen Posten nur ohne offene Frage: Bei einem Kandidaten ohne Bestätigung stünde
    # sonst eine ungeprüfte Bankverbindung unter seinem Namen im Zahllauf.
    supplier_iban = None if match.needs_confirmation else document_iban
    # Für „wie zuletzt“ reicht ein einzelner Kandidat: Das Konto bleibt ein Vorschlag, der
    # Partner wird dadurch nicht zugeordnet.
    history_partner = partner
    if history_partner is None and len(match.candidates) == 1:
        history_partner = match.candidates[0].partner
    tax_code = _tax_code_for(session, company_id, extraction)
    tax_lines = propose_tax_lines(
        session, company_id, extraction.tax_lines, reverse_charge=extraction.reverse_charge
    )
    notes: list[str] = []
    if extraction.reverse_charge:
        notes.append("Reverse Charge: ohne Vorsteuer-Code, § 13b-Steuer selbst buchen.")

    common = dict(
        partner_match=match,
        supplier_iban=supplier_iban,
        proposed_partner=proposed_partner,
    )
    history = _history_line(
        session, company_id, set(accounts), partner=history_partner, supplier=extraction.supplier
    )
    if history is not None:
        line, entry = history
        account = accounts[line.account_id]
        who = history_partner.name if history_partner else extraction.supplier
        reason = (
            f"Wie zuletzt bei {who}: Buchung {entry.posting_number} vom "
            f"{entry.entry_date:%d.%m.%Y} auf {account.code} {account.name}."
        )
        return ReceiptBookingProposal(
            source="history",
            reason=" ".join([reason, *notes]),
            account=account,
            tax_code=tax_code,
            partner=partner,
            history_entry_id=entry.id,
            tax_lines=tax_lines,
            **common,
        )

    if extraction.suggested_account_code:
        account = next(
            (a for a in accounts.values() if a.code == extraction.suggested_account_code),
            None,
        )
        if account is not None:
            reason = f"KI-Vorschlag aus dem Kontenplan: {account.code} {account.name}."
            return ReceiptBookingProposal(
                source="ki",
                reason=" ".join([reason, *notes]),
                account=account,
                tax_code=tax_code,
                partner=partner,
                tax_lines=tax_lines,
                **common,
            )

    return ReceiptBookingProposal(
        source=None,
        reason=" ".join(["Kein Kontovorschlag – bitte Konto wählen.", *notes]),
        tax_code=tax_code,
        partner=partner,
        tax_lines=tax_lines,
        **common,
    )
