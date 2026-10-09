"""Zusammenfassende Meldung (ZM): EU-Umsätze je Kunden-USt-IdNr.

Grundlage sind dieselben Erlöszeilen wie in der UStVA (DATEV-Kontenfunktion des
Erlöskontos, ``revenue_kennzahl``):

* Kz 41 (steuerfreie innergemeinschaftliche Lieferungen) → Art „L“,
* Kz 42 (Lieferungen des ersten Abnehmers im Dreiecksgeschäft) → Art „D“,
* Kz 21 (sonstige Leistungen an EU-Unternehmer, § 18b) → Art „S“.

Den Kunden liefert der Geschäftspartner der Buchung (in der Regel auf der
Debitorenzeile); ein Storno ohne eigenen Partner gehört zum Kunden der stornierten
Buchung (festgeschriebene Stornos lassen sich nicht ergänzen). Gemeldet werden
Ländercode, USt-IdNr. und die Summe je Art, Gutschriften mindern sie. Wie in der
UStVA entfallen Centbeträge. Fehlt der Partner oder seine EU-USt-IdNr., erscheint
die Zeile unter ``missing``. Die Übermittlung an das BZSt (ELSTER) liegt außerhalb
von OpenBuchhaltung.

Meldezeitraum (§ 18a Abs. 8 UStG): sonstige Leistungen nach dem Leistungsdatum
der Buchung, Lieferungen nach der Rechnung (Buchungsdatum), spätestens im Monat
nach der Lieferung (``app.services.tax_period``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.account_chart_check import detect_company_chart
from app.services.vat_returns import (
    EU_COUNTRY_CODES,
    partner_vat_country,
    revenue_kennzahl,
    revenue_tax_rule,
    vat_entries,
)
from domain.models import BusinessPartner, JournalEntryLine

ZERO = Decimal("0.00")
ZM_KINDS = {"41": "L", "42": "D", "21": "S"}
ZM_KIND_LABELS = {
    "L": "Innergemeinschaftliche Lieferung",
    "D": "Dreiecksgeschäft (erster Abnehmer)",
    "S": "Sonstige Leistung",
}


@dataclass(slots=True)
class ZmResult:
    rows: list[dict[str, object]] = field(default_factory=list)
    # Erlöszeilen ohne zuordenbare EU-USt-IdNr. des Kunden.
    missing: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _normalized_vat_id(vat_id: str | None) -> str:
    return (vat_id or "").replace(" ", "").upper()


def compute_zm(
    *, session: Session, company_id: int, date_from: date, date_to: date
) -> ZmResult:
    """ZM-Zeilen (Land, USt-IdNr., Art, Betrag) für den Meldezeitraum."""
    chart = detect_company_chart(session=session, company_id=company_id)
    partners = {
        partner.id: partner
        for partner in session.execute(
            select(BusinessPartner).where(BusinessPartner.company_id == company_id)
        ).scalars()
    }
    totals: dict[tuple[str, str, str], Decimal] = {}
    names: dict[tuple[str, str, str], str] = {}
    result = ZmResult()

    entries = vat_entries(session, company_id, date_from, date_to, False)
    # Partner der stornierten Originalbuchungen (auch außerhalb des Zeitraums).
    originals = {entry.reversal_of_id for entry in entries.values() if entry.reversal_of_id}
    original_partners: dict[int, set[int]] = {}
    if originals:
        for journal_entry_id, partner_id in session.execute(
            select(JournalEntryLine.journal_entry_id, JournalEntryLine.partner_id).where(
                JournalEntryLine.journal_entry_id.in_(originals),
                JournalEntryLine.partner_id.is_not(None),
            )
        ):
            original_partners.setdefault(journal_entry_id, set()).add(partner_id)

    for entry in entries.values():
        partner_ids = {row.partner_id for row in entry.rows if row.partner_id is not None}
        if not partner_ids and entry.reversal_of_id is not None:
            partner_ids = original_partners.get(entry.reversal_of_id, set())
        partner = partners.get(next(iter(partner_ids))) if len(partner_ids) == 1 else None
        for row in entry.rows:
            if row.line_account_type not in {"revenue", "income"}:
                continue
            if row.tax_code_id is not None and row.rate != ZERO:
                continue
            kennzahl = revenue_kennzahl(chart, row.account_code)
            kind = ZM_KINDS.get(kennzahl or "")
            if kind is None:
                continue
            if not date_from <= entry.tax_point(revenue_tax_rule(kennzahl)) <= date_to:
                continue
            amount = row.credit_amount - row.debit_amount
            vat_id = _normalized_vat_id(partner.vat_id if partner else None)
            country = (
                partner_vat_country(partner.country_code, vat_id) if partner is not None else None
            )
            if len(partner_ids) > 1:
                reason = "mehrere Geschäftspartner in der Buchung"
            elif partner is None:
                reason = "kein Geschäftspartner zugeordnet"
            elif not vat_id:
                reason = "Geschäftspartner ohne USt-IdNr."
            elif country == "DE" or country not in EU_COUNTRY_CODES:
                reason = "USt-IdNr. nicht aus einem anderen EU-Mitgliedstaat"
            else:
                reason = None
            if reason is not None:
                result.missing.append(
                    {
                        "posting_number": entry.posting_number,
                        "entry_date": entry.entry_date.isoformat(),
                        "account_code": row.account_code,
                        "kind": kind,
                        "amount": str(amount.quantize(ZERO)),
                        "partner_name": partner.name if partner is not None else None,
                        "reason": reason,
                    }
                )
                continue
            key = (country, vat_id, kind)
            totals[key] = totals.get(key, ZERO) + amount
            names.setdefault(key, partner.name)

    for (country, vat_id, kind), amount in sorted(totals.items()):
        result.rows.append(
            {
                "country_code": country,
                "vat_id": vat_id,
                "kind": kind,
                "kind_label": ZM_KIND_LABELS[kind],
                "partner_name": names[(country, vat_id, kind)],
                "amount": str(amount.quantize(ZERO)),
                "amount_euro": str(amount.quantize(Decimal("1"), rounding=ROUND_DOWN)),
            }
        )
    if result.missing:
        result.warnings.append(
            f"{len(result.missing)} ZM-relevante Erlöszeile(n) ohne EU-USt-IdNr. des Kunden: "
            "Geschäftspartner mit USt-IdNr. auf der Debitorenzeile der Buchung zuordnen."
        )
    if chart is None:
        result.warnings.append(
            "Kontenrahmen nicht erkannt: ZM-relevante Erlöskonten (8125/4125, 8336/4336 …) "
            "lassen sich nicht zuordnen."
        )
    return result
