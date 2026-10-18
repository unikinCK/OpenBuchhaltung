"""Verbuchung von Eingangsrechnungen: Netto an Aufwand, Steuer an das
Steuerkonto des Steuercodes, Brutto an den Kreditor.

Belege mit mehreren Steuersätzen (Restaurant 7 %/19 %, Hotel Übernachtung/
Frühstück) werden je Steuersatz aufgeteilt (:class:`InvoiceTaxSplit`): je Satz
eine Aufwands- und eine Steuerzeile. Trinkgeld ist eine eigene Aufwandszeile
ohne Steuer.

Gemeinsame Logik für Beleg-OCR und E-Rechnungs-Import — Web und API rufen
dieselbe Funktion auf (UI/API/MCP-Parität), statt die Zeilenkonstruktion
vierfach zu duplizieren.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from domain.models import Account, BusinessPartner, Company, JournalEntry, TaxCode


class IncomingInvoiceError(ValueError):
    """Fachlicher Fehler beim Verbuchen einer Eingangsrechnung.

    ``code`` erlaubt der API eine eigene Statuscode-/Meldungszuordnung.
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class InvoiceTaxSplit:
    """Ein Steuersatz einer Eingangsrechnung: Netto auf Aufwand, Steuer per Steuercode.

    Ohne ``tax_code_id`` wird bei Steuer der Vorsteuer-Code mit ``rate`` gewählt;
    ohne ``expense_account_id`` gilt das Aufwandskonto der Rechnung.
    """

    net_amount: Decimal
    tax_amount: Decimal
    tax_code_id: int | None = None
    rate: Decimal | None = None
    expense_account_id: int | None = None


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def parse_tax_splits(raw: object) -> list[InvoiceTaxSplit] | None:
    """Liest die Aufteilung je Steuersatz aus REST-/MCP-Daten.

    Erwartet eine Liste von Objekten mit ``net_amount``, ``tax_amount`` und
    optional ``tax_code_id``, ``rate``, ``expense_account_id`` (Format von
    ``booking_proposal.tax_lines``). Zeilen ohne Betrag werden übergangen;
    ``None``/leere Liste bedeutet: keine Aufteilung. Fehler als ``ValueError``.
    """
    if raw in (None, ""):
        return None
    if not isinstance(raw, list):
        raise ValueError("tax_lines muss eine Liste sein.")
    splits: list[InvoiceTaxSplit] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Jede Zeile in tax_lines muss ein Objekt sein.")
        net_amount = parse_decimal(str(item.get("net_amount") or "0"))
        tax_amount = parse_decimal(str(item.get("tax_amount") or "0"))
        if net_amount == 0 and tax_amount == 0:
            continue
        rate = item.get("rate")
        splits.append(
            InvoiceTaxSplit(
                net_amount=net_amount,
                tax_amount=tax_amount,
                tax_code_id=_optional_int(item.get("tax_code_id")),
                rate=parse_decimal(str(rate), places=None) if rate not in (None, "") else None,
                expense_account_id=_optional_int(item.get("expense_account_id")),
            )
        )
    return splits or None


def input_tax_code_for_rate(session: Session, company_id: int, rate: Decimal) -> TaxCode | None:
    """Erster aktiver Vorsteuer-Code der Gesellschaft mit diesem Steuersatz."""
    return session.execute(
        select(TaxCode)
        .where(
            TaxCode.company_id == company_id,
            TaxCode.is_active.is_(True),
            TaxCode.kind == "input",
            TaxCode.rate == rate,
        )
        .order_by(TaxCode.id)
        .limit(1)
    ).scalar_one_or_none()


def invoice_gross_amount(
    net_amount: Decimal,
    tax_amount: Decimal,
    tax_splits: Sequence[InvoiceTaxSplit] | None = None,
    tip_amount: Decimal | None = None,
) -> Decimal:
    """Bruttobetrag der Rechnung: Netto + Steuer (bzw. Summe der Aufteilung) + Trinkgeld."""
    zero = Decimal("0.00")
    if tax_splits:
        total = sum((split.net_amount + split.tax_amount for split in tax_splits), zero)
    else:
        total = net_amount + tax_amount
    if tip_amount is not None and tip_amount > zero:
        total += tip_amount
    return total.quantize(zero)


def tax_splits_audit(tax_splits: Sequence[InvoiceTaxSplit] | None) -> list[dict] | None:
    """Aufteilung je Steuersatz für den Audit-Log (Beträge als Strings)."""
    if not tax_splits:
        return None
    return [
        {
            "net_amount": str(split.net_amount),
            "tax_amount": str(split.tax_amount),
            "tax_code_id": split.tax_code_id,
            "rate": str(split.rate) if split.rate is not None else None,
            "expense_account_id": split.expense_account_id,
        }
        for split in tax_splits
    ]


def _format_rate(rate: Decimal) -> str:
    text = format(rate.normalize(), "f")
    return text.replace(".", ",")


def book_incoming_invoice(
    *,
    session: Session,
    company: Company,
    entry_date: date,
    description: str,
    expense_account_id: int,
    creditor_account_id: int,
    net_amount: Decimal,
    tax_amount: Decimal,
    gross_amount: Decimal | None = None,
    tax_code_id: int | None = None,
    expense_line_description: str | None = None,
    tax_line_description: str | None = None,
    cost_center_id: int | None = None,
    profit_center_id: int | None = None,
    partner_id: int | None = None,
    tax_splits: Sequence[InvoiceTaxSplit] | None = None,
    tip_amount: Decimal | None = None,
    changed_by: str,
    commit: bool = True,
) -> JournalEntry:
    """Bucht eine Eingangsrechnung; die Steuer wird exakt übernommen
    (keine Auto-Expansion über den Steuercode), damit das Brutto aufgeht.

    Mit ``tax_splits`` (mehrere Steuersätze) ersetzen die Zeilen der Aufteilung
    ``net_amount``/``tax_amount``/``tax_code_id``: je Satz eine Aufwands- und
    – bei Steuer – eine Steuerzeile. ``tip_amount`` (Trinkgeld) wird als eigene
    Aufwandszeile ohne Steuer auf das Aufwandskonto gebucht. Das Brutto ergibt
    sich dann aus der Summe aller Zeilen.

    ``partner_id`` (Lieferant) steht auf der Aufwandszeile und – sofern das
    Gegenkonto kein Kreditoren-Sammelkonto ist oder der Partner eine
    Kreditorennummer hat – auch auf der Gegenzeile.

    Mit ``commit=False`` kann der Aufrufer Verknüpfungen (Beleg, Vorschlag)
    atomar mit der Buchung committen.
    """
    zero = Decimal("0.00")
    splits = list(tax_splits or []) or [
        InvoiceTaxSplit(net_amount=net_amount, tax_amount=tax_amount, tax_code_id=tax_code_id)
    ]
    tip = tip_amount if tip_amount is not None and tip_amount > zero else zero
    if tax_splits or tip or gross_amount is None:
        gross_amount = invoice_gross_amount(net_amount, tax_amount, tax_splits, tip)

    def company_account(account_id: int | None) -> Account:
        account = session.get(Account, account_id) if account_id else None
        if account is None or account.company_id != company.id:
            raise IncomingInvoiceError(
                "Ausgewähltes Konto gehört nicht zur Gesellschaft.",
                code="account_not_found",
            )
        return account

    expense_account = company_account(expense_account_id)
    creditor_account = company_account(creditor_account_id)
    partner = session.get(BusinessPartner, partner_id) if partner_id else None
    if partner_id and (partner is None or partner.company_id != company.id):
        raise IncomingInvoiceError(
            "Geschäftspartner gehört nicht zur Gesellschaft.", code="partner_not_found"
        )
    creditor_partner_id = (
        partner.id
        if partner is not None
        and (creditor_account.subledger != "creditor" or partner.creditor_number)
        else None
    )

    multi_rate = len(splits) > 1
    lines: list[JournalLineInput] = []
    for split in splits:
        tax_code = _split_tax_code(session, company, split) if split.tax_amount > zero else None
        line_text = expense_line_description
        if multi_rate:
            rate = tax_code.rate if tax_code is not None else split.rate
            label = f"{_format_rate(rate)} %" if rate is not None else "ohne USt"
            line_text = (
                f"{expense_line_description} ({label})"
                if expense_line_description
                else f"Netto {label}"
            )
        account = (
            company_account(split.expense_account_id)
            if split.expense_account_id
            else expense_account
        )
        lines.append(
            JournalLineInput(
                account_id=account.id,
                debit_amount=split.net_amount,
                credit_amount=zero,
                description=line_text,
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
                partner_id=partner.id if partner else None,
            )
        )
        if tax_code is not None:
            lines.append(
                JournalLineInput(
                    account_id=tax_code.vat_account_id,
                    debit_amount=split.tax_amount,
                    credit_amount=zero,
                    description=(
                        tax_line_description
                        if tax_line_description and not multi_rate
                        else f"Vorsteuer ({tax_code.code})"
                    ),
                )
            )
    if tip:
        lines.append(
            JournalLineInput(
                account_id=expense_account.id,
                debit_amount=tip,
                credit_amount=zero,
                description="Trinkgeld (ohne USt)",
                cost_center_id=cost_center_id,
                profit_center_id=profit_center_id,
                partner_id=partner.id if partner else None,
            )
        )
    lines.append(
        JournalLineInput(
            account_id=creditor_account.id,
            debit_amount=zero,
            credit_amount=gross_amount,
            partner_id=creditor_partner_id,
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
            lines=lines,
        ),
        commit=commit,
    )


def _split_tax_code(session: Session, company: Company, split: InvoiceTaxSplit) -> TaxCode:
    """Steuercode einer Zeile mit Steuer: angegeben oder per Steuersatz ermittelt."""
    if split.tax_code_id:
        tax_code = session.get(TaxCode, split.tax_code_id)
    elif split.rate is not None:
        tax_code = input_tax_code_for_rate(session, company.id, split.rate)
        if tax_code is None:
            raise IncomingInvoiceError(
                f"Kein Vorsteuer-Code mit {_format_rate(split.rate)} % vorhanden.",
                code="tax_code_not_found",
            )
    else:
        tax_code = None
    if tax_code is None or tax_code.company_id != company.id:
        raise IncomingInvoiceError(
            "Für die Steuer bitte einen gültigen Steuercode wählen.",
            code="tax_code_not_found",
        )
    if tax_code.vat_account_id is None:
        raise IncomingInvoiceError(
            f"Steuercode {tax_code.code} hat kein Steuerkonto.",
            code="tax_code_without_vat_account",
        )
    return tax_code
