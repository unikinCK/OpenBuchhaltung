"""Belegabgleich: Beleg-Uploads mit vorhandenen Buchungen abgleichen.

Der Abgleich läuft in drei Stufen:

1. **Belegdaten gewinnen**: Der gespeicherte Beleg wird erneut durch die
   OCR-Pipeline (:func:`app.services.receipt_ocr.analyze_document`) geschickt,
   um Bruttobetrag, Datum, Lieferant usw. zu erhalten.
2. **Kandidaten finden** (regelbasiert): Buchungen der Gesellschaft, deren
   Zeilenbetrag zum Bruttobetrag passt (bzw. die jüngsten Buchungen, wenn kein
   Betrag erkannt wurde). Buchungen ohne Beleg stehen vorn; Buchungen mit
   bereits verknüpften Belegen bleiben Kandidaten, weil eine Buchung mehrere
   Belege haben kann (z. B. Rechnung und Zahlungsbeleg).
3. **Entscheiden**: Ist ein LLM-Endpoint konfiguriert, wählt das LLM aus den
   Kandidaten die passende Buchung (oder keine) und begründet die Wahl.
   Ohne LLM entscheidet die Regel "eindeutiger Betragstreffer". Ohne Treffer
   entsteht ein Vorschlag für eine **neue Buchung** aus den Belegdaten.

Jeder Vorschlag wird als :class:`domain.models.ReceiptMatchSuggestion`
persistiert und muss vom Anwender freigegeben (dabei änderbar) oder
abgelehnt werden. LLM-Fehler blockieren nie: es greift die regelbasierte
Entscheidung, der Grund vermerkt das.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.incoming_invoice import (
    IncomingInvoiceError,
    InvoiceTaxSplit,
    book_incoming_invoice,
    invoice_gross_amount,
    tax_splits_audit,
)
from app.services.receipt_ocr import (
    ReceiptExtraction,
    ReceiptLLMError,
    ReceiptOCRError,
    _collect_response_text,
    _parse_llm_json,
    analyze_document,
    post_llm_payload,
    sanitize_text,
)
from app.services.receipt_proposals import account_choices, propose_receipt_booking
from domain.formatting import format_amount
from domain.models import (
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    ReceiptMatchSuggestion,
)

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")

# Höchster Abstand (Tage) zwischen Belegdatum und Buchungsdatum, bei dem ein
# Beleg regelbasiert einer Buchung zugeordnet wird, die schon Belege hat
# (z. B. Zahlungsbeleg nach Rechnung mit Zahlungsziel).
ADDITIONAL_RECEIPT_MAX_DAYS = 60

STATUS_OPEN = "offen"
STATUS_APPROVED = "freigegeben"
STATUS_REJECTED = "abgelehnt"

TYPE_MATCH = "match"
TYPE_NEW_BOOKING = "new_booking"


class ReceiptMatchError(ValueError):
    """Raised when a receipt-match suggestion cannot be created or decided."""


def _clean_db_text(value: str | None) -> str | None:
    """Bereinigt Textfelder vor dem Persistieren (defensiv, zusätzlich zur OCR-Stufe).

    PostgreSQL lehnt NUL-Bytes in Text- und JSON-Spalten mit einem DataError ab;
    hier wird das unabhängig davon abgefangen, woher der Wert stammt.
    """
    if value is None:
        return None
    cleaned = sanitize_text(value).strip()
    return cleaned or None


@dataclass(slots=True)
class MatchDecision:
    """Ergebnis der Abgleichsentscheidung (LLM oder regelbasiert)."""

    journal_entry_id: int | None
    confidence: str
    reason: str
    llm_used: bool


# ---------------------------------------------------------------------------
# Stufe 2: Kandidaten regelbasiert finden
# ---------------------------------------------------------------------------


def entry_gross_total(entry: JournalEntry) -> Decimal:
    """Bruttosumme einer Buchung (Summe der Sollseite)."""
    total = sum((line.debit_amount for line in entry.lines), Decimal("0.00"))
    return total.quantize(_CENT)


def find_candidate_entries(
    *,
    session: Session,
    company_id: int,
    gross_amount: Decimal | None,
    limit: int = 15,
    tip_amount: Decimal | None = None,
) -> list[JournalEntry]:
    """Buchungen der Gesellschaft, die als Match in Frage kommen.

    Mit erkanntem Bruttobetrag zählen nur Buchungen mit einer Zeile in dieser
    Höhe; ohne Betrag werden die jüngsten Buchungen geliefert, damit ein LLM
    anhand von Text/Datum entscheiden kann. Buchungen ohne Beleg stehen vorn,
    Buchungen mit Belegen folgen (weiterer Beleg zur selben Buchung).
    Stornobuchungen werden übersprungen. Mit Trinkgeld zählt auch der
    tatsächlich gezahlte Betrag (Brutto + Trinkgeld).
    """
    linked = select(Document.journal_entry_id).where(Document.journal_entry_id.is_not(None))
    has_documents = case((JournalEntry.id.in_(linked), 1), else_=0)
    stmt = select(JournalEntry).where(
        JournalEntry.company_id == company_id,
        JournalEntry.reversal_of_id.is_(None),
    )
    if gross_amount is not None and gross_amount > 0:
        # IN-Subquery statt JOIN + DISTINCT: PostgreSQL verlangt bei DISTINCT,
        # dass ORDER-BY-Ausdrücke (hier has_documents) in der Auswahl stehen.
        amounts = [gross_amount]
        if tip_amount:
            amounts.append((gross_amount + tip_amount).quantize(_CENT))
        matching_lines = select(JournalEntryLine.journal_entry_id).where(
            or_(
                JournalEntryLine.debit_amount.in_(amounts),
                JournalEntryLine.credit_amount.in_(amounts),
            )
        )
        stmt = stmt.where(JournalEntry.id.in_(matching_lines))
    stmt = stmt.order_by(
        has_documents, JournalEntry.entry_date.desc(), JournalEntry.id.desc()
    ).limit(limit)
    return session.execute(stmt).scalars().all()


def linked_document_counts(session: Session, journal_entry_ids: list[int]) -> dict[int, int]:
    """Anzahl aktueller, bereits verknüpfter Belege je Buchung."""
    if not journal_entry_ids:
        return {}
    return dict(
        session.execute(
            select(Document.journal_entry_id, func.count())
            .where(
                Document.journal_entry_id.in_(journal_entry_ids),
                Document.is_current.is_(True),
            )
            .group_by(Document.journal_entry_id)
        ).all()
    )


# ---------------------------------------------------------------------------
# Stufe 3: Entscheidung – LLM mit regelbasiertem Fallback
# ---------------------------------------------------------------------------

_MATCH_INSTRUCTION = (
    "Du gleichst einen Beleg (Eingangsrechnung/Quittung) einer deutschen "
    "Buchhaltung mit vorhandenen Buchungen ab. Du erhältst die aus dem Beleg "
    "extrahierten Daten und eine Kandidatenliste von Buchungen. Wähle die "
    "Buchung, die denselben Geschäftsvorfall abbildet (Betrag, Datum nahe "
    "beieinander, Lieferant/Verwendungszweck passend), oder keine, wenn "
    "nichts überzeugend passt. Eine Buchung kann mehrere Belege haben "
    "(z. B. Rechnung und Zahlungsbeleg): Kandidaten mit vorhandene_belege > 0 "
    "sind nur zu wählen, wenn der Beleg eindeutig denselben Vorgang ergänzt; "
    "sonst Buchungen ohne Beleg bevorzugen. Antworte ausschließlich mit einem JSON-Objekt "
    'mit exakt diesen Feldern: {"journal_entry_id": number|null, '
    '"confidence": "hoch"|"mittel"|"niedrig", "reason": string}. '
    "reason ist eine kurze deutsche Begründung."
)


def _receipt_summary(extraction: ReceiptExtraction) -> str:
    fields = {
        "lieferant": extraction.supplier,
        "rechnungsnummer": extraction.invoice_number,
        "rechnungsdatum": (
            extraction.invoice_date.isoformat() if extraction.invoice_date else None
        ),
        "netto": str(extraction.net_amount) if extraction.net_amount is not None else None,
        "steuer": str(extraction.tax_amount) if extraction.tax_amount is not None else None,
        "brutto": str(extraction.gross_amount) if extraction.gross_amount is not None else None,
        "trinkgeld": str(extraction.tip_amount) if extraction.tip_amount else None,
        "waehrung": extraction.currency_code,
        "leistung": extraction.description,
    }
    return json.dumps(fields, ensure_ascii=False)


def _candidate_summary(
    candidates: list[JournalEntry], document_counts: dict[int, int] | None = None
) -> str:
    counts = document_counts or {}
    rows = [
        {
            "journal_entry_id": entry.id,
            "buchungsnummer": entry.posting_number,
            "datum": entry.entry_date.isoformat(),
            "text": entry.description,
            "brutto": str(entry_gross_total(entry)),
            "vorhandene_belege": counts.get(entry.id, 0),
        }
        for entry in candidates
    ]
    return json.dumps(rows, ensure_ascii=False)


def choose_match_llm(
    *,
    extraction: ReceiptExtraction,
    candidates: list[JournalEntry],
    endpoint_url: str,
    model: str,
    api_key: str | None = None,
    document_counts: dict[int, int] | None = None,
) -> MatchDecision:
    """Lässt ein LLM aus den Kandidaten die passende Buchung wählen (oder keine)."""
    user_text = (
        f"Belegdaten: {_receipt_summary(extraction)}\n"
        f"Belegtext (Auszug):\n{extraction.raw_text[:4000]}\n\n"
        f"Kandidaten-Buchungen: {_candidate_summary(candidates, document_counts)}"
    )
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": _MATCH_INSTRUCTION}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_text}],
            },
        ],
        "metadata": {"source": "openbuchhaltung-receipt-matching"},
        "temperature": 0,
    }
    body = post_llm_payload(endpoint_url, payload, api_key, label="Abgleich-LLM")

    data = _parse_llm_json(_collect_response_text(body))
    raw_entry_id = data.get("journal_entry_id")
    if isinstance(raw_entry_id, str) and raw_entry_id.strip().isdigit():
        raw_entry_id = int(raw_entry_id.strip())
    entry_id = raw_entry_id if isinstance(raw_entry_id, int) else None
    valid_ids = {entry.id for entry in candidates}
    if entry_id is not None and entry_id not in valid_ids:
        raise ReceiptLLMError(
            f"Abgleich-LLM nannte eine Buchung außerhalb der Kandidaten ({entry_id})."
        )
    confidence = str(data.get("confidence") or "niedrig")
    if confidence not in {"hoch", "mittel", "niedrig"}:
        confidence = "niedrig"
    reason = str(data.get("reason") or "").strip() or "Keine Begründung vom LLM."
    return MatchDecision(
        journal_entry_id=entry_id, confidence=confidence, reason=reason, llm_used=True
    )


def choose_match_rule_based(
    *,
    extraction: ReceiptExtraction,
    candidates: list[JournalEntry],
    document_counts: dict[int, int] | None = None,
) -> MatchDecision:
    """Regelbasierte Entscheidung: eindeutiger Betragstreffer gewinnt.

    Buchungen ohne Beleg haben Vorrang. Passen nur Buchungen, die schon Belege
    haben, wird der Beleg als weiterer Nachweis zur zeitlich nächsten Buchung
    vorgeschlagen – aber nur mit bekanntem Belegdatum und höchstens
    :data:`ADDITIONAL_RECEIPT_MAX_DAYS` Tagen Abstand.
    """
    if extraction.gross_amount is None or extraction.gross_amount <= 0 or not candidates:
        return MatchDecision(
            journal_entry_id=None,
            confidence="niedrig",
            reason="Keine Buchung mit passendem Betrag gefunden.",
            llm_used=False,
        )
    counts = document_counts or {}
    without_documents = [entry for entry in candidates if not counts.get(entry.id)]
    if not without_documents:
        return _choose_additional_receipt(
            extraction=extraction, candidates=candidates, counts=counts
        )
    candidates = without_documents
    best = candidates[0]
    if len(candidates) == 1:
        return MatchDecision(
            journal_entry_id=best.id,
            confidence="mittel",
            reason=(
                f"Betrag {format_amount(extraction.gross_amount)} stimmt mit Buchung "
                f"{best.posting_number} überein (regelbasiert)."
            ),
            llm_used=False,
        )
    # Mehrere Betragstreffer: bei bekanntem Belegdatum das nächstgelegene wählen.
    if extraction.invoice_date is not None:
        best = min(
            candidates,
            key=lambda entry: abs((entry.entry_date - extraction.invoice_date).days),
        )
    return MatchDecision(
        journal_entry_id=best.id,
        confidence="niedrig",
        reason=(
            f"{len(candidates)} Buchungen mit Betrag "
            f"{format_amount(extraction.gross_amount)} gefunden; "
            f"{best.posting_number} liegt dem Belegdatum am nächsten. Bitte prüfen."
        ),
        llm_used=False,
    )


def _choose_additional_receipt(
    *,
    extraction: ReceiptExtraction,
    candidates: list[JournalEntry],
    counts: dict[int, int],
) -> MatchDecision:
    """Betrag passt nur zu Buchungen mit Belegen: weiterer Beleg zur nächsten Buchung."""
    if extraction.invoice_date is None:
        return MatchDecision(
            journal_entry_id=None,
            confidence="niedrig",
            reason=(
                f"Betrag {format_amount(extraction.gross_amount)} passt nur zu Buchungen, "
                "die bereits Belege haben; ohne erkanntes Belegdatum keine Zuordnung."
            ),
            llm_used=False,
        )
    best = min(
        candidates,
        key=lambda entry: abs((entry.entry_date - extraction.invoice_date).days),
    )
    distance = abs((best.entry_date - extraction.invoice_date).days)
    if distance > ADDITIONAL_RECEIPT_MAX_DAYS:
        return MatchDecision(
            journal_entry_id=None,
            confidence="niedrig",
            reason=(
                f"Betrag {format_amount(extraction.gross_amount)} passt nur zu Buchungen mit "
                f"vorhandenen Belegen, die nächste ({best.posting_number}) liegt "
                f"{distance} Tage vom Belegdatum entfernt."
            ),
            llm_used=False,
        )
    existing = counts.get(best.id, 0)
    return MatchDecision(
        journal_entry_id=best.id,
        confidence="niedrig",
        reason=(
            f"Betrag {format_amount(extraction.gross_amount)} stimmt mit Buchung "
            f"{best.posting_number} überein, die bereits {existing} Beleg(e) hat – als "
            "weiterer Beleg (z. B. Zahlungsbeleg) vorgeschlagen. Bitte prüfen."
        ),
        llm_used=False,
    )


# ---------------------------------------------------------------------------
# Workflow: Vorschlag anlegen, freigeben (ggf. geändert), ablehnen
# ---------------------------------------------------------------------------


def _open_suggestion_exists(session: Session, document_id: int) -> bool:
    count = session.execute(
        select(func.count())
        .select_from(ReceiptMatchSuggestion)
        .where(
            ReceiptMatchSuggestion.document_id == document_id,
            ReceiptMatchSuggestion.status == STATUS_OPEN,
        )
    ).scalar_one()
    return count > 0


def create_match_suggestion(
    *,
    session: Session,
    company_id: int,
    document_id: int,
    changed_by: str,
    ocr_endpoint: str | None = None,
    ocr_model: str = "gpt-4.1-mini",
    llm_endpoint: str | None = None,
    llm_model: str = "gpt-4.1-mini",
    ocr_api_key: str | None = None,
    llm_api_key: str | None = None,
) -> ReceiptMatchSuggestion:
    """Analysiert einen unverknüpften Beleg und legt einen Abgleichsvorschlag an."""
    company = session.get(Company, company_id)
    if company is None:
        raise ReceiptMatchError("Gesellschaft nicht gefunden.")

    document = session.get(Document, document_id)
    if document is None or document.company_id != company.id:
        raise ReceiptMatchError("Beleg nicht gefunden.")
    if not document.is_current:
        raise ReceiptMatchError("Der Beleg wurde durch eine neuere Version ersetzt.")
    if document.journal_entry_id is not None:
        raise ReceiptMatchError("Der Beleg ist bereits mit einer Buchung verknüpft.")
    if _open_suggestion_exists(session, document.id):
        raise ReceiptMatchError(
            "Für diesen Beleg existiert bereits ein offener Abgleichsvorschlag."
        )

    storage_path = Path(document.storage_key)
    try:
        file_bytes = storage_path.read_bytes()
    except OSError as exc:
        raise ReceiptMatchError(
            f"Die Belegdatei {document.file_name} ist nicht lesbar."
        ) from exc

    try:
        extraction = analyze_document(
            file_bytes=file_bytes,
            mime_type=document.mime_type,
            file_name=document.file_name,
            ocr_endpoint=ocr_endpoint,
            ocr_model=ocr_model,
            llm_endpoint=llm_endpoint,
            llm_model=llm_model,
            ocr_api_key=ocr_api_key,
            llm_api_key=llm_api_key,
            account_choices=account_choices(session, company.id),
        )
    except ReceiptOCRError as exc:
        raise ReceiptMatchError(f"Beleg konnte nicht ausgelesen werden: {exc}") from exc

    candidates = find_candidate_entries(
        session=session,
        company_id=company.id,
        gross_amount=extraction.gross_amount,
        tip_amount=extraction.tip_amount,
    )
    document_counts = linked_document_counts(session, [entry.id for entry in candidates])

    decision: MatchDecision
    if llm_endpoint and candidates:
        try:
            decision = choose_match_llm(
                extraction=extraction,
                candidates=candidates,
                endpoint_url=llm_endpoint,
                model=llm_model,
                api_key=llm_api_key,
                document_counts=document_counts,
            )
        except ReceiptLLMError as exc:
            decision = choose_match_rule_based(
                extraction=extraction, candidates=candidates, document_counts=document_counts
            )
            decision.reason = f"{decision.reason} (LLM-Abgleich nicht möglich: {exc})"
    else:
        decision = choose_match_rule_based(
            extraction=extraction, candidates=candidates, document_counts=document_counts
        )

    suggestion_type = TYPE_MATCH if decision.journal_entry_id is not None else TYPE_NEW_BOOKING
    if suggestion_type == TYPE_NEW_BOOKING and not extraction.has_booking_basis:
        decision.reason = (
            f"{decision.reason} Es wurde auch kein Betrag erkannt – die Buchung "
            "bitte manuell vervollständigen."
        )

    proposal = propose_receipt_booking(session, company.id, extraction)
    suggestion = ReceiptMatchSuggestion(
        tenant_id=company.tenant_id,
        company_id=company.id,
        document_id=document.id,
        suggestion_type=suggestion_type,
        journal_entry_id=decision.journal_entry_id,
        confidence=decision.confidence,
        reason=sanitize_text(decision.reason).strip(),
        llm_used=decision.llm_used,
        status=STATUS_OPEN,
        supplier=_clean_db_text(extraction.supplier),
        invoice_number=_clean_db_text(extraction.invoice_number),
        invoice_date=extraction.invoice_date,
        net_amount=extraction.net_amount,
        tax_amount=extraction.tax_amount,
        gross_amount=extraction.gross_amount,
        tax_rate=extraction.tax_rate,
        currency_code=_clean_db_text(extraction.currency_code) or "EUR",
        supplier_vat_id=_clean_db_text(extraction.supplier_vat_id),
        description=_clean_db_text(extraction.description),
        tip_amount=extraction.tip_amount,
        reverse_charge=extraction.reverse_charge,
        expense_account_id=proposal.account.id if proposal.account else None,
        tax_code_id=proposal.tax_code.id if proposal.tax_code else None,
        partner_id=proposal.partner.id if proposal.partner else None,
        proposal_source=proposal.source,
        proposal_reason=_clean_db_text(proposal.reason),
        tax_lines=[
            {key: value for key, value in line.as_dict().items() if key != "tax_code"}
            for line in proposal.tax_lines
        ]
        or None,
    )
    session.add(suggestion)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="receipt_match_suggestion",
        entity_id=str(suggestion.id),
        action="created",
        changed_by=changed_by,
        payload={
            "document_id": document.id,
            "suggestion_type": suggestion.suggestion_type,
            "journal_entry_id": suggestion.journal_entry_id,
            "confidence": suggestion.confidence,
            "llm_used": suggestion.llm_used,
            "gross_amount": (
                str(suggestion.gross_amount) if suggestion.gross_amount is not None else None
            ),
            "expense_account_id": suggestion.expense_account_id,
            "proposal_source": suggestion.proposal_source,
            "tax_lines": suggestion.tax_lines,
        },
    )
    session.commit()
    session.refresh(suggestion)
    return suggestion


def _load_open_suggestion(
    session: Session, suggestion_id: int
) -> tuple[ReceiptMatchSuggestion, Document]:
    suggestion = session.get(ReceiptMatchSuggestion, suggestion_id)
    if suggestion is None:
        raise ReceiptMatchError("Abgleichsvorschlag nicht gefunden.")
    if suggestion.status != STATUS_OPEN:
        raise ReceiptMatchError("Der Abgleichsvorschlag ist bereits entschieden.")
    document = session.get(Document, suggestion.document_id)
    if document is None:
        raise ReceiptMatchError("Zugehöriger Beleg wurde nicht gefunden.")
    return suggestion, document


def approve_match_suggestion(
    *,
    session: Session,
    suggestion_id: int,
    changed_by: str,
    journal_entry_id: int | None = None,
) -> ReceiptMatchSuggestion:
    """Gibt einen Match-Vorschlag frei und verknüpft den Beleg mit der Buchung.

    ``journal_entry_id`` erlaubt es, vor der Freigabe eine andere Buchung als
    die vorgeschlagene zu wählen.
    """
    suggestion, document = _load_open_suggestion(session, suggestion_id)
    if suggestion.suggestion_type != TYPE_MATCH:
        raise ReceiptMatchError(
            "Dieser Vorschlag ist eine neue Buchung – bitte über die Buchungsmaske freigeben."
        )
    if document.journal_entry_id is not None:
        raise ReceiptMatchError("Der Beleg ist inzwischen bereits mit einer Buchung verknüpft.")

    target_id = journal_entry_id or suggestion.journal_entry_id
    if target_id is None:
        raise ReceiptMatchError("Keine Buchung zum Verknüpfen angegeben.")
    entry = session.get(JournalEntry, target_id)
    if entry is None or entry.company_id != suggestion.company_id:
        raise ReceiptMatchError("Buchung nicht gefunden.")

    overridden = target_id != suggestion.journal_entry_id
    suggested_entry_id = suggestion.journal_entry_id
    document.journal_entry_id = entry.id
    suggestion.journal_entry_id = entry.id
    suggestion.status = STATUS_APPROVED
    suggestion.decided_at = datetime.now(timezone.utc)
    suggestion.decided_by = changed_by

    log_audit_event(
        session=session,
        tenant_id=suggestion.tenant_id,
        company_id=suggestion.company_id,
        entity_type="receipt_match_suggestion",
        entity_id=str(suggestion.id),
        action="approved",
        changed_by=changed_by,
        payload={
            "document_id": document.id,
            "journal_entry_id": entry.id,
            "posting_number": entry.posting_number,
            "suggested_journal_entry_id": suggested_entry_id,
            "overridden": overridden,
        },
    )
    session.commit()
    session.refresh(suggestion)
    return suggestion


def book_new_booking_suggestion(
    *,
    session: Session,
    suggestion_id: int,
    changed_by: str,
    expense_account_id: int | None,
    creditor_account_id: int,
    net_amount: Decimal,
    tax_amount: Decimal,
    tax_code_id: int | None = None,
    entry_date: date | None = None,
    description: str | None = None,
    cost_center_id: int | None = None,
    profit_center_id: int | None = None,
    partner_id: int | None = None,
    tax_splits: Sequence[InvoiceTaxSplit] | None = None,
    tip_amount: Decimal | None = None,
) -> tuple[ReceiptMatchSuggestion, JournalEntry]:
    """Gibt einen New-Booking-Vorschlag frei: erzeugt die Buchung und verknüpft den Beleg.

    Beträge, Konten und Datum sind gegenüber dem Vorschlag frei änderbar. Ohne
    Angabe gelten Konto, Steuercode (bei Steuer) und Geschäftspartner aus dem
    Kontierungsvorschlag. ``tax_splits`` bucht mehrere Steuersätze (ersetzt
    ``net_amount``/``tax_amount``/``tax_code_id``), ``tip_amount`` Trinkgeld als
    eigene Zeile ohne Steuer – siehe :func:`book_incoming_invoice`.
    """
    suggestion, document = _load_open_suggestion(session, suggestion_id)
    if suggestion.suggestion_type != TYPE_NEW_BOOKING:
        raise ReceiptMatchError(
            "Dieser Vorschlag verweist auf eine vorhandene Buchung – bitte dort freigeben."
        )
    if document.journal_entry_id is not None:
        raise ReceiptMatchError("Der Beleg ist inzwischen bereits mit einer Buchung verknüpft.")

    expense_account_id = expense_account_id or suggestion.expense_account_id
    if expense_account_id is None:
        raise ReceiptMatchError("Bitte ein Aufwandskonto wählen.")

    zero = Decimal("0.00")
    tip = tip_amount if tip_amount is not None and tip_amount > zero else zero
    gross_amount = invoice_gross_amount(net_amount, tax_amount, tax_splits, tip)
    if gross_amount <= zero:
        raise ReceiptMatchError("Netto- und Steuerbetrag ergeben keinen positiven Bruttobetrag.")
    if not tax_splits and tax_amount > zero:
        tax_code_id = tax_code_id or suggestion.tax_code_id

    booking_date = entry_date or suggestion.invoice_date or date.today()
    default_text = " – ".join(
        part
        for part in (
            suggestion.supplier,
            f"Rg. {suggestion.invoice_number}" if suggestion.invoice_number else None,
            suggestion.description,
        )
        if part
    )
    booking_text = (description or "").strip() or (
        f"Belegbuchung (Abgleich): {default_text}" if default_text else "Belegbuchung (Abgleich)"
    )

    company = session.get(Company, suggestion.company_id)
    # commit=False: Buchung, Belegverknüpfung und Vorschlagsstatus werden
    # gemeinsam persistiert — sonst bliebe bei einem Fehler eine Buchung ohne
    # Beleg zurück und der Vorschlag könnte erneut gebucht werden.
    try:
        entry = book_incoming_invoice(
            session=session,
            company=company,
            entry_date=booking_date,
            description=booking_text,
            expense_account_id=expense_account_id,
            creditor_account_id=creditor_account_id,
            net_amount=net_amount,
            tax_amount=tax_amount,
            tax_code_id=tax_code_id,
            expense_line_description=booking_text,
            cost_center_id=cost_center_id,
            profit_center_id=profit_center_id,
            partner_id=partner_id or suggestion.partner_id,
            tax_splits=tax_splits,
            tip_amount=tip,
            changed_by=changed_by,
            commit=False,
        )
    except IncomingInvoiceError as exc:
        raise ReceiptMatchError(str(exc)) from exc

    document.journal_entry_id = entry.id
    document.document_date = booking_date
    suggestion.journal_entry_id = entry.id
    suggestion.status = STATUS_APPROVED
    suggestion.decided_at = datetime.now(timezone.utc)
    suggestion.decided_by = changed_by

    log_audit_event(
        session=session,
        tenant_id=suggestion.tenant_id,
        company_id=suggestion.company_id,
        entity_type="receipt_match_suggestion",
        entity_id=str(suggestion.id),
        action="booked",
        changed_by=changed_by,
        payload={
            "document_id": document.id,
            "journal_entry_id": entry.id,
            "posting_number": entry.posting_number,
            "net_amount": str(net_amount),
            "tax_amount": str(tax_amount),
            "gross_amount": str(gross_amount),
            "tax_lines": tax_splits_audit(tax_splits),
            "tip_amount": str(tip) if tip else None,
            "cost_center_id": cost_center_id,
            "profit_center_id": profit_center_id,
        },
    )
    session.commit()
    session.refresh(suggestion)
    session.refresh(entry)
    return suggestion, entry


def reject_suggestion(
    *, session: Session, suggestion_id: int, changed_by: str
) -> ReceiptMatchSuggestion:
    """Lehnt einen offenen Abgleichsvorschlag ab (Beleg bleibt unverknüpft)."""
    suggestion, document = _load_open_suggestion(session, suggestion_id)
    del document
    suggestion.status = STATUS_REJECTED
    suggestion.decided_at = datetime.now(timezone.utc)
    suggestion.decided_by = changed_by

    log_audit_event(
        session=session,
        tenant_id=suggestion.tenant_id,
        company_id=suggestion.company_id,
        entity_type="receipt_match_suggestion",
        entity_id=str(suggestion.id),
        action="rejected",
        changed_by=changed_by,
        payload={
            "document_id": suggestion.document_id,
            "suggestion_type": suggestion.suggestion_type,
        },
    )
    session.commit()
    session.refresh(suggestion)
    return suggestion
