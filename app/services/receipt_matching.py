"""Belegabgleich: Beleg-Uploads mit vorhandenen Buchungen abgleichen.

Der Abgleich läuft in drei Stufen:

1. **Belegdaten gewinnen**: Der gespeicherte Beleg wird erneut durch die
   OCR-Pipeline (:func:`app.services.receipt_ocr.analyze_document`) geschickt,
   um Bruttobetrag, Datum, Lieferant usw. zu erhalten.
2. **Kandidaten finden** (regelbasiert): Buchungen der Gesellschaft ohne
   Belegverknüpfung, deren Zeilenbetrag zum Bruttobetrag passt (bzw. die
   jüngsten unverknüpften Buchungen, wenn kein Betrag erkannt wurde).
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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.receipt_ocr import (
    ReceiptExtraction,
    ReceiptLLMError,
    ReceiptOCRError,
    _collect_response_text,
    _parse_llm_json,
    analyze_document,
)
from domain.models import (
    Account,
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    ReceiptMatchSuggestion,
    TaxCode,
)

_CENT = Decimal("0.01")

STATUS_OPEN = "offen"
STATUS_APPROVED = "freigegeben"
STATUS_REJECTED = "abgelehnt"

TYPE_MATCH = "match"
TYPE_NEW_BOOKING = "new_booking"


class ReceiptMatchError(ValueError):
    """Raised when a receipt-match suggestion cannot be created or decided."""


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
) -> list[JournalEntry]:
    """Unverknüpfte Buchungen der Gesellschaft, die als Match in Frage kommen.

    Mit erkanntem Bruttobetrag zählen nur Buchungen mit einer Zeile in dieser
    Höhe; ohne Betrag werden die jüngsten unverknüpften Buchungen geliefert,
    damit ein LLM anhand von Text/Datum entscheiden kann. Stornobuchungen
    werden übersprungen.
    """
    linked = select(Document.journal_entry_id).where(Document.journal_entry_id.is_not(None))
    stmt = select(JournalEntry).where(
        JournalEntry.company_id == company_id,
        JournalEntry.id.not_in(linked),
        JournalEntry.reversal_of_id.is_(None),
    )
    if gross_amount is not None and gross_amount > 0:
        stmt = (
            stmt.join(
                JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id
            )
            .where(
                or_(
                    JournalEntryLine.debit_amount == gross_amount,
                    JournalEntryLine.credit_amount == gross_amount,
                )
            )
            .distinct()
        )
    stmt = stmt.order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc()).limit(limit)
    return session.execute(stmt).scalars().all()


# ---------------------------------------------------------------------------
# Stufe 3: Entscheidung – LLM mit regelbasiertem Fallback
# ---------------------------------------------------------------------------

_MATCH_INSTRUCTION = (
    "Du gleichst einen Beleg (Eingangsrechnung/Quittung) einer deutschen "
    "Buchhaltung mit vorhandenen Buchungen ab. Du erhältst die aus dem Beleg "
    "extrahierten Daten und eine Kandidatenliste von Buchungen. Wähle die "
    "Buchung, die denselben Geschäftsvorfall abbildet (Betrag, Datum nahe "
    "beieinander, Lieferant/Verwendungszweck passend), oder keine, wenn "
    "nichts überzeugend passt. Antworte ausschließlich mit einem JSON-Objekt "
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
        "waehrung": extraction.currency_code,
    }
    return json.dumps(fields, ensure_ascii=False)


def _candidate_summary(candidates: list[JournalEntry]) -> str:
    rows = [
        {
            "journal_entry_id": entry.id,
            "buchungsnummer": entry.posting_number,
            "datum": entry.entry_date.isoformat(),
            "text": entry.description,
            "brutto": str(entry_gross_total(entry)),
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
) -> MatchDecision:
    """Lässt ein LLM aus den Kandidaten die passende Buchung wählen (oder keine)."""
    user_text = (
        f"Belegdaten: {_receipt_summary(extraction)}\n"
        f"Belegtext (Auszug):\n{extraction.raw_text[:4000]}\n\n"
        f"Kandidaten-Buchungen: {_candidate_summary(candidates)}"
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
    }
    request = Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ReceiptLLMError(
            f"Abgleich-LLM antwortete mit HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise ReceiptLLMError("Abgleich-LLM ist nicht erreichbar.") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptLLMError("Abgleich-LLM lieferte kein gültiges JSON.") from exc

    data = _parse_llm_json(_collect_response_text(body))
    raw_entry_id = data.get("journal_entry_id")
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
    *, extraction: ReceiptExtraction, candidates: list[JournalEntry]
) -> MatchDecision:
    """Regelbasierte Entscheidung: eindeutiger Betragstreffer gewinnt."""
    if extraction.gross_amount is None or extraction.gross_amount <= 0 or not candidates:
        return MatchDecision(
            journal_entry_id=None,
            confidence="niedrig",
            reason="Keine Buchung mit passendem Betrag gefunden.",
            llm_used=False,
        )
    best = candidates[0]
    if len(candidates) == 1:
        return MatchDecision(
            journal_entry_id=best.id,
            confidence="mittel",
            reason=(
                f"Betrag {extraction.gross_amount} stimmt mit Buchung "
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
            f"{len(candidates)} Buchungen mit Betrag {extraction.gross_amount} gefunden; "
            f"{best.posting_number} liegt dem Belegdatum am nächsten. Bitte prüfen."
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
        )
    except ReceiptOCRError as exc:
        raise ReceiptMatchError(f"Beleg konnte nicht ausgelesen werden: {exc}") from exc

    candidates = find_candidate_entries(
        session=session, company_id=company.id, gross_amount=extraction.gross_amount
    )

    decision: MatchDecision
    if llm_endpoint and candidates:
        try:
            decision = choose_match_llm(
                extraction=extraction,
                candidates=candidates,
                endpoint_url=llm_endpoint,
                model=llm_model,
            )
        except ReceiptLLMError as exc:
            decision = choose_match_rule_based(extraction=extraction, candidates=candidates)
            decision.reason = f"{decision.reason} (LLM-Abgleich nicht möglich: {exc})"
    else:
        decision = choose_match_rule_based(extraction=extraction, candidates=candidates)

    suggestion_type = TYPE_MATCH if decision.journal_entry_id is not None else TYPE_NEW_BOOKING
    if suggestion_type == TYPE_NEW_BOOKING and not extraction.has_booking_basis:
        decision.reason = (
            f"{decision.reason} Es wurde auch kein Betrag erkannt – die Buchung "
            "bitte manuell vervollständigen."
        )

    suggestion = ReceiptMatchSuggestion(
        tenant_id=company.tenant_id,
        company_id=company.id,
        document_id=document.id,
        suggestion_type=suggestion_type,
        journal_entry_id=decision.journal_entry_id,
        confidence=decision.confidence,
        reason=decision.reason,
        llm_used=decision.llm_used,
        status=STATUS_OPEN,
        supplier=extraction.supplier,
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        net_amount=extraction.net_amount,
        tax_amount=extraction.tax_amount,
        gross_amount=extraction.gross_amount,
        tax_rate=extraction.tax_rate,
        currency_code=extraction.currency_code,
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
    expense_account_id: int,
    creditor_account_id: int,
    net_amount: Decimal,
    tax_amount: Decimal,
    tax_code_id: int | None = None,
    entry_date: date | None = None,
    description: str | None = None,
    cost_center_id: int | None = None,
    profit_center_id: int | None = None,
) -> tuple[ReceiptMatchSuggestion, JournalEntry]:
    """Gibt einen New-Booking-Vorschlag frei: erzeugt die Buchung und verknüpft den Beleg.

    Beträge, Konten und Datum sind gegenüber dem Vorschlag frei änderbar.
    """
    suggestion, document = _load_open_suggestion(session, suggestion_id)
    if suggestion.suggestion_type != TYPE_NEW_BOOKING:
        raise ReceiptMatchError(
            "Dieser Vorschlag verweist auf eine vorhandene Buchung – bitte dort freigeben."
        )
    if document.journal_entry_id is not None:
        raise ReceiptMatchError("Der Beleg ist inzwischen bereits mit einer Buchung verknüpft.")

    expense_account = session.get(Account, expense_account_id)
    creditor_account = session.get(Account, creditor_account_id)
    for account in (expense_account, creditor_account):
        if account is None or account.company_id != suggestion.company_id:
            raise ReceiptMatchError("Ausgewähltes Konto gehört nicht zur Gesellschaft.")

    zero = Decimal("0.00")
    gross_amount = (net_amount + tax_amount).quantize(zero)
    if gross_amount <= zero:
        raise ReceiptMatchError("Netto- und Steuerbetrag ergeben keinen positiven Bruttobetrag.")

    booking_date = entry_date or suggestion.invoice_date or date.today()
    booking_text = (description or "").strip() or (
        f"Belegbuchung (Abgleich): {suggestion.supplier}"
        if suggestion.supplier
        else "Belegbuchung (Abgleich)"
    )

    lines = [
        JournalLineInput(
            account_id=expense_account.id,
            debit_amount=net_amount,
            credit_amount=zero,
            description=booking_text,
            cost_center_id=cost_center_id,
            profit_center_id=profit_center_id,
        )
    ]
    if tax_amount > zero:
        tax_code = session.get(TaxCode, tax_code_id) if tax_code_id else None
        if tax_code is None or tax_code.company_id != suggestion.company_id:
            raise ReceiptMatchError("Für die Steuer bitte einen gültigen Steuercode wählen.")
        if tax_code.vat_account_id is None:
            raise ReceiptMatchError(f"Steuercode {tax_code.code} hat kein Steuerkonto.")
        lines.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                debit_amount=tax_amount,
                credit_amount=zero,
                description=f"Vorsteuer ({tax_code.code})",
            )
        )
    lines.append(
        JournalLineInput(
            account_id=creditor_account.id,
            debit_amount=zero,
            credit_amount=gross_amount,
        )
    )

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=suggestion.company_id,
            entry_date=booking_date,
            description=booking_text,
            status="posted",
            changed_by=changed_by,
            lines=lines,
        ),
    )

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
