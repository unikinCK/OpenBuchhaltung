"""Verbuchung von Eingangsrechnungen: Netto an Aufwand, Steuer an das
Steuerkonto des Steuercodes, Brutto an den Kreditor.

Gemeinsame Logik für Beleg-OCR und E-Rechnungs-Import — Web und API rufen
dieselbe Funktion auf (UI/API/MCP-Parität), statt die Zeilenkonstruktion
vierfach zu duplizieren.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, Company, JournalEntry, TaxCode


class IncomingInvoiceError(ValueError):
    """Fachlicher Fehler beim Verbuchen einer Eingangsrechnung.

    ``code`` erlaubt der API eine eigene Statuscode-/Meldungszuordnung.
    """

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


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
    changed_by: str,
    commit: bool = True,
) -> JournalEntry:
    """Bucht eine Eingangsrechnung; die Steuer wird exakt übernommen
    (keine Auto-Expansion über den Steuercode), damit das Brutto aufgeht.

    Mit ``commit=False`` kann der Aufrufer Verknüpfungen (Beleg, Vorschlag)
    atomar mit der Buchung committen.
    """
    zero = Decimal("0.00")
    if gross_amount is None:
        gross_amount = (net_amount + tax_amount).quantize(zero)

    expense_account = session.get(Account, expense_account_id)
    creditor_account = session.get(Account, creditor_account_id)
    for account in (expense_account, creditor_account):
        if account is None or account.company_id != company.id:
            raise IncomingInvoiceError(
                "Ausgewähltes Konto gehört nicht zur Gesellschaft.",
                code="account_not_found",
            )

    lines = [
        JournalLineInput(
            account_id=expense_account.id,
            debit_amount=net_amount,
            credit_amount=zero,
            description=expense_line_description,
            cost_center_id=cost_center_id,
            profit_center_id=profit_center_id,
        )
    ]
    if tax_amount > zero:
        tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
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
        lines.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                debit_amount=tax_amount,
                credit_amount=zero,
                description=tax_line_description or f"Vorsteuer ({tax_code.code})",
            )
        )
    lines.append(
        JournalLineInput(
            account_id=creditor_account.id,
            debit_amount=zero,
            credit_amount=gross_amount,
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
