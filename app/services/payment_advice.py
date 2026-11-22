"""Zahlungsavise: auslesen, offenen Posten zuordnen, Differenzen erklären, ausgleichen.

Ein Kunde oder Zentralregulierer überweist mehrere Rechnungen in einer Summe und schickt ein
Avis mit den Positionen und seinen Abzügen (Skonto, Delkredere, Bonus, Konventionalstrafe …).

Grundsatz (ADR-012): **Die KI liest, das Programm rechnet.**

1. Text holen: PDF-Textebene je Seite, sonst OCR; CSV/Text in Abschnitten.
2. Auslesen: Die KI schreibt je Seite die Positionen *wörtlich* als JSON ab, Beträge als Text.
3. Prüfen: Jede Nummer und jeder Betrag muss im Seitentext stehen, sonst ein zweiter Versuch
   mit den Fehlern; was dann noch fehlt, macht das Avis rot.
4. Zuordnen: Belegnummer → Referenz des offenen Postens; ohne Nummer nur über den Betrag beim
   erkannten Partner (gelb).
5. Erklären: Skonto aus der Spalte oder als glatter Prozentsatz, Delkredere, Abzüge ohne Posten
   über Stichwörter in mehreren Sprachen.
6. Ampel: grün = vollständig und ohne Annahme erklärt, gelb = erklärt mit Annahme (Betrag,
   Skontofrist), rot = Summe geht nicht auf, Posten fehlt, Abzug unklar.

Der Ausgleich bucht den Bankumsatz in einer Buchung gegen die Posten, das Skonto (mit
Steuerkorrektur über den Steuercode) und die übrigen Abzüge, und gleicht die Posten aus.
"""

from __future__ import annotations

import io
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.bank_import import net_from_gross
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    lock_cash_basis_company,
)
from app.services.llm_settings import llm_timeout_floor
from app.services.open_items import OpenItemError, settle_open_item
from app.services.receipt_ocr import (
    ReceiptLLMError,
    ReceiptOCRError,
    _collect_response_text,
    _parse_llm_json,
    extract_document_text,
    post_llm_payload,
    sanitize_text,
)
from domain.models import (
    Account,
    BankTransaction,
    BusinessPartner,
    Company,
    Document,
    OpenItem,
    PaymentAdvice,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError

logger = logging.getLogger(__name__)

STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_APPLIED = "applied"

GREEN, YELLOW, RED = "green", "yellow", "red"
LIGHT_LABELS = {GREEN: "grün", YELLOW: "gelb", RED: "rot"}
_LIGHT_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# Arten einer Avis-Position (Schema der KI) und ihre interne Bedeutung.
KIND_INVOICE = "rechnung"
KIND_CREDIT_NOTE = "gutschrift"
KIND_DEDUCTION = "abzug"
KIND_FEE = "gebuehr"
KINDS = (KIND_INVOICE, KIND_CREDIT_NOTE, KIND_DEDUCTION, KIND_FEE)

# Kategorien eines Abzugs. Skonto mindert Erlös und Umsatzsteuer, die übrigen gehen auf das
# Konto für sonstige Abzüge (Steuercode nach Wahl).
CAT_SKONTO = "skonto"
CAT_DELKREDERE = "delkredere"
CAT_BONUS = "bonus"
CAT_PENALTY = "konventionalstrafe"
CAT_CLAIM = "reklamation"
CAT_RETURN = "retoure"
CAT_FEE = "gebuehr"
CAT_UNCLEAR = "unklar"
CATEGORY_LABELS = {
    CAT_SKONTO: "Skonto",
    CAT_DELKREDERE: "Delkredere",
    CAT_BONUS: "Bonus/Konditionen",
    CAT_PENALTY: "Konventionalstrafe",
    CAT_CLAIM: "Reklamation",
    CAT_RETURN: "Retoure/Rücklastschrift",
    CAT_FEE: "Gebühr",
    CAT_UNCLEAR: "Abzug unklar",
}
# Stichwörter je Kategorie (kleingeschrieben, mehrsprachig). Die Reihenfolge zählt:
# „Delkredere-Gebühr“ ist Delkredere, nicht Gebühr.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (CAT_SKONTO, (
        "skonto", "korting", "sconto", "discount", "kontantrabat", "descuento", "escompte",
        "cash disc",
    )),
    (CAT_DELKREDERE, (
        "delkredere", "delcredere", "dienstleistungsverg", "betalingsgaranti",
        "servicios financieros",
    )),
    (CAT_PENALTY, ("konventional", "vertragsstrafe", "pönale", "poenale", "penalty", "penalit")),
    (CAT_BONUS, (
        "bonus", "werbekosten", "wkz", "rabatt", "kondition", "teilabrechnung", "rebate",
        "% gs",
    )),
    (CAT_CLAIM, (
        "reklamation", "beschädigt", "beschaedigt", "fehlmenge", "preisdifferenz", "damaged",
        "shortage", "claim", "mängel",
    )),
    (CAT_RETURN, ("retoure", "rücksendung", "ruecksendung", "rücklastschrift", "return")),
    (CAT_FEE, ("gebühr", "gebuehr", "fee", "kosten", "charge", "spesen")),
)
# Glatte Skontosätze. 3,5 % fehlt bewusst: Das sind oft 2 % Skonto und 1,5 % Delkredere.
SKONTO_RATES = (Decimal("1"), Decimal("2"), Decimal("2.5"), Decimal("3"), Decimal("4"))
# Skonto bei mehr Tagen zwischen Rechnung und Zahlung: Frist prüfen (gelb).
SKONTO_DAYS = 30
# Zeilen, die keine Positionen sind (Überträge und Summen).
_SUBTOTAL_RE = re.compile(
    r"\b(übertrag|uebertrag|zwischensumme|summe|gesamtsumme|total|traslado|carry\s*forward|"
    r"subtotal|saldo)\b",
    re.IGNORECASE,
)

_CENT = Decimal("0.01")
_TOLERANCE = Decimal("0.02")
# So viele Zeichen bekommt die KI je Aufruf; längere Seiten werden geteilt.
_PAGE_CHARS = 6000
_MAX_PAGES = 30
JOB_STALE_AFTER = timedelta(minutes=45)
STALE_ERROR = "Das Auslesen wurde abgebrochen (Server neu gestartet). Bitte erneut starten."


class PaymentAdviceError(ValueError):
    """Fachlicher Fehler beim Avis (Auslesen, Zuordnen, Ausgleich)."""


# ---------------------------------------------------------------------------
# Beträge, Daten, Text
# ---------------------------------------------------------------------------


def parse_amount(raw: object) -> Decimal | None:
    """Betrag aus dem Avis: ``1.234,56``, ``1,234.56``, ``17,36-``, ``-17,36``, ``(17,36)``.

    Das Vorzeichen bleibt erhalten; unlesbare Werte ergeben ``None``.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        return Decimal(str(raw)).quantize(_CENT, rounding=ROUND_HALF_UP)
    text = str(raw).strip()
    if not text:
        return None
    negative = (
        text.endswith("-")
        or text.startswith("-")
        or text.startswith("−")
        or (text.startswith("(") and text.endswith(")"))
    )
    digits = re.sub(r"[^0-9.,]", "", text)
    if not re.search(r"\d", digits):
        return None
    last_dot, last_comma = digits.rfind("."), digits.rfind(",")
    decimal_pos = max(last_dot, last_comma)
    if decimal_pos != -1 and 1 <= len(digits) - decimal_pos - 1 <= 2:
        whole = re.sub(r"[.,]", "", digits[:decimal_pos])
        number = f"{whole or '0'}.{digits[decimal_pos + 1:]}"
    else:
        number = re.sub(r"[.,]", "", digits)
    try:
        value = Decimal(number).quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None
    return -value if negative else value


_DATE_PATTERNS = (
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), ("y", "m", "d")),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"), ("d", "m", "y")),
    (re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2})$"), ("d", "m", "yy")),
)


def parse_date(raw: object) -> date | None:
    """Datum aus dem Avis (``15.10.26``, ``15.10.2026``, ``2026-10-15``, ``15/10/2026``)."""
    text = str(raw or "").strip()
    for pattern, order in _DATE_PATTERNS:
        match = pattern.match(text)
        if not match:
            continue
        parts = dict(zip(order, match.groups(), strict=True))
        year = int(parts.get("y") or 2000 + int(parts["yy"]))
        try:
            return date(year, int(parts["m"]), int(parts["d"]))
        except ValueError:
            return None
    return None


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _amount_core(raw: str) -> str:
    """Betrag ohne Vorzeichen, Klammern und Währung – so muss er im Seitentext stehen."""
    return _compact(re.sub(r"[+\-−()€$]|eur|chf|usd|dkk|sek|czk|pln", "", str(raw), flags=re.I))


def _in_text(raw: str, compact_text: str, *, amount: bool = False) -> bool:
    value = _amount_core(raw) if amount else _compact(raw)
    return not value or value in compact_text


def normalize_reference(raw: object) -> str:
    """Belegnummer für den Vergleich: nur Buchstaben und Ziffern, Großschreibung."""
    return re.sub(r"[^0-9A-Za-z]", "", str(raw or "")).upper()


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_CENT))


def _money(value: Decimal) -> str:
    """Betrag für Meldungen im deutschen Format."""
    text = f"{value.quantize(_CENT):,.2f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------------
# Text holen
# ---------------------------------------------------------------------------


def _split_long(text: str) -> list[str]:
    """Teilt Text an Zeilengrenzen in Abschnitte von höchstens ``_PAGE_CHARS`` Zeichen."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if size + len(line) + 1 > _PAGE_CHARS and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line[:_PAGE_CHARS])
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _pdf_page_texts(file_bytes: bytes) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        return [sanitize_text(page.extract_text() or "") for page in reader.pages]
    except Exception:  # noqa: BLE001 – kaputtes PDF: Rückfall auf die allgemeine Extraktion
        logger.info("Avis: PDF-Seiten nicht einzeln lesbar, Rückfall auf Gesamttext.")
        return []


def advice_pages(
    *,
    file_bytes: bytes,
    mime_type: str,
    file_name: str,
    ocr_endpoint: str | None = None,
    ocr_model: str = "gpt-4.1-mini",
    ocr_api_key: str | None = None,
) -> list[str]:
    """Text des Avis, aufgeteilt in Seiten bzw. Abschnitte für je einen KI-Aufruf."""
    pages: list[str] = []
    is_pdf = (mime_type or "").lower() == "application/pdf" or file_name.lower().endswith(".pdf")
    if is_pdf:
        pages = [page for page in _pdf_page_texts(file_bytes) if len(page.strip()) >= 20]
    if not pages:
        try:
            text, _source = extract_document_text(
                file_bytes=file_bytes,
                mime_type=mime_type,
                file_name=file_name,
                ocr_endpoint=ocr_endpoint,
                ocr_model=ocr_model,
                ocr_api_key=ocr_api_key,
            )
        except ReceiptOCRError as exc:
            raise PaymentAdviceError(f"Das Avis konnte nicht gelesen werden: {exc}") from exc
        pages = [text]
    chunks = [chunk for page in pages for chunk in _split_long(page)]
    if not chunks:
        raise PaymentAdviceError("Das Avis enthält keinen lesbaren Text.")
    if len(chunks) > _MAX_PAGES:
        raise PaymentAdviceError(
            f"Das Avis hat {len(chunks)} Seiten bzw. Abschnitte, höchstens {_MAX_PAGES} gehen."
        )
    return chunks


# ---------------------------------------------------------------------------
# Auslesen durch die KI
# ---------------------------------------------------------------------------

_POSITION_FIELDS = (
    "belegnr",
    "kunden_belegnr",
    "datum",
    "rechnungsbetrag",
    "skonto",
    "delkredere",
    "sonstiger_abzug",
    "zahlbetrag",
    "art",
    "text",
)
_HEADER_FIELDS = (
    "zahler",
    "im_auftrag_von",
    "avis_nummer",
    "zahldatum",
    "waehrung",
    "zahlbetrag_gesamt",
)
_AMOUNT_FIELDS = ("rechnungsbetrag", "skonto", "delkredere", "sonstiger_abzug", "zahlbetrag")

_STRING = {"type": "string"}
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        **{name: _STRING for name in _HEADER_FIELDS},
        "abzuege_gesamt": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"bezeichnung": _STRING, "betrag": _STRING},
                "required": ["bezeichnung", "betrag"],
                "additionalProperties": False,
            },
        },
        "positionen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **{name: _STRING for name in _POSITION_FIELDS if name != "art"},
                    "art": {"type": "string", "enum": list(KINDS)},
                },
                "required": list(_POSITION_FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": [*_HEADER_FIELDS, "abzuege_gesamt", "positionen"],
    "additionalProperties": False,
}

EXTRACTION_INSTRUCTION = """\
Du liest eine Seite eines Zahlungsavises, das ein Kunde an {company} geschickt hat. Gib die \
Positionen dieser Seite als JSON nach dem vorgegebenen Schema zurück.

Grundregeln
1. Schreibe jede Nummer und jeden Betrag Zeichen für Zeichen so ab, wie er im Text steht, \
also mit Punkt, Komma und Minuszeichen. Rechne nichts um, runde nichts und ergänze nichts.
2. Ein Feld, das es auf der Seite nicht gibt, bleibt leer (""). Erfinde keine Werte.
3. Eine Position ist eine Zeile mit einem eigenen Beleg und Betrag. Nicht dazu gehören \
Zwischensummen, „Übertrag“, „Summe“, „Total“, Spaltenüberschriften und die Zahlungszeile mit dem \
Gesamtbetrag. Der Gesamtbetrag gehört nur in zahlbetrag_gesamt.
4. Steht derselbe Beleg zweimal da, etwa einmal mit dem Rechnungsbetrag und einmal mit einem \
Minusbetrag, dann sind das zwei Positionen.
5. Folgezeilen (Bestellnummer, Text unter der Belegzeile) gehören in das Feld text der Position \
darüber.

Felder einer Position
- belegnr: Rechnungs- oder Gutschriftsnummer von {company} (des Zahlungsempfängers), wie sie \
im Avis steht. Leer, wenn es keine gibt.
- kunden_belegnr: eigene Belegnummer des Kunden (Belastungsanzeige, Dokumentnummer).
- datum: Beleg- oder Rechnungsdatum, wie es dasteht.
- rechnungsbetrag: Brutto- bzw. Rechnungsbetrag vor Abzügen.
- skonto: Skonto (Korting, Sconto, Discount, Kontantrabat) dieser Zeile.
- delkredere: Delkredere, DV (Dienstleistungsvergütung) oder Delcredere dieser Zeile.
- sonstiger_abzug: andere Abzüge in dieser Zeile.
- zahlbetrag: Zahl- oder Nettobetrag dieser Zeile.
- art, genau einer dieser Werte:
  - rechnung: eine Rechnung von {company} wird bezahlt.
  - gutschrift: eine Gutschrift von {company} wird verrechnet.
  - abzug: Der Kunde zieht selbst etwas ab, zum Beispiel Belastung, Konventionalstrafe, Bonus, \
Werbekostenzuschuss, Preisdifferenz, beschädigte Ware oder eine Minuszeile zu einer Rechnung.
  - gebuehr: Rechnung eines Finanzdienstleisters oder Zentralregulierers über Gebühren oder \
Delkredere.
- text: Beschreibung oder Folgezeile, kurz.

Kopffelder (nur ausfüllen, wenn sie auf dieser Seite stehen)
- zahler: Firma, die das Avis schickt bzw. zahlt.
- im_auftrag_von: bei Zentralregulierern („gezahlt im Auftrag von“) der eigentliche Kunde.
- avis_nummer, zahldatum, waehrung.
- zahlbetrag_gesamt: der überwiesene Gesamtbetrag des Avises nach allen Abzügen.
- abzuege_gesamt: Abzüge, die nur bei der Summe und nicht je Zeile stehen, zum Beispiel \
„Less Discount“ oder Delkredere auf die Summe; je Eintrag bezeichnung und betrag.
"""


def _extraction_payload(page_text: str, *, model: str, company: str, feedback: str) -> dict:
    user_text = page_text
    if feedback:
        user_text += (
            "\n\n---\nDeine letzte Abschrift dieser Seite hatte Fehler:\n" + feedback
            + "\nBitte die Seite noch einmal vollständig und wörtlich abschreiben."
        )
    return {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {"type": "input_text", "text": EXTRACTION_INSTRUCTION.format(company=company)}
                ],
            },
            {"role": "user", "content": [{"type": "input_text", "text": user_text}]},
        ],
        "metadata": {"source": "openbuchhaltung-payment-advice"},
        "temperature": 0,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "zahlungsavis",
                "schema": EXTRACTION_SCHEMA,
                "strict": True,
            }
        },
    }


def call_extraction_llm(
    page_text: str,
    *,
    endpoint_url: str,
    model: str,
    api_key: str | None,
    company: str,
    feedback: str = "",
) -> dict:
    """Eine Seite durch die KI abschreiben lassen (OpenAI-``/responses``-Format)."""
    payload = _extraction_payload(page_text, model=model, company=company, feedback=feedback)
    body = post_llm_payload(endpoint_url, payload, api_key, label="KI-Endpoint")
    return _parse_llm_json(_collect_response_text(body))


def _clean_page(data: dict) -> dict:
    """Antwort der KI auf das Schema bringen: nur bekannte Felder, alles als Text."""
    page: dict = {name: str(data.get(name) or "").strip() for name in _HEADER_FIELDS}
    page["abzuege_gesamt"] = [
        {
            "bezeichnung": str(row.get("bezeichnung") or "").strip(),
            "betrag": str(row.get("betrag") or "").strip(),
        }
        for row in data.get("abzuege_gesamt") or []
        if isinstance(row, dict)
    ]
    positions = []
    for row in data.get("positionen") or []:
        if not isinstance(row, dict):
            continue
        position = {name: str(row.get(name) or "").strip() for name in _POSITION_FIELDS}
        position["art"] = position["art"].lower()
        positions.append(position)
    page["positionen"] = positions
    return page


def _is_subtotal(position: dict) -> bool:
    if position["belegnr"] or position["kunden_belegnr"]:
        return False
    return bool(_SUBTOTAL_RE.search(position["text"]))


def check_page(page: dict, page_text: str) -> list[str]:
    """Prüft eine Abschrift gegen den Seitentext. Liefert die Fehler als Sätze."""
    compact = _compact(page_text)
    problems: list[str] = []
    for row_number, position in enumerate(page["positionen"], start=1):
        label = position["belegnr"] or position["kunden_belegnr"] or f"Zeile {row_number}"
        if position["art"] not in KINDS:
            problems.append(f"{label}: Art „{position['art']}“ ist nicht erlaubt.")
        for name in ("belegnr", "kunden_belegnr"):
            if not _in_text(position[name], compact):
                problems.append(f"{label}: Nummer „{position[name]}“ steht nicht auf der Seite.")
        for name in _AMOUNT_FIELDS:
            raw = position[name]
            if not raw:
                continue
            if parse_amount(raw) is None:
                problems.append(f"{label}: {name} „{raw}“ ist kein Betrag.")
            elif not _in_text(raw, compact, amount=True):
                problems.append(f"{label}: {name} „{raw}“ steht nicht auf der Seite.")
        if not any(position[name] for name in _AMOUNT_FIELDS):
            problems.append(f"{label}: kein Betrag erfasst.")
    total = page["zahlbetrag_gesamt"]
    if total and (parse_amount(total) is None or not _in_text(total, compact, amount=True)):
        problems.append(f"Gesamtbetrag „{total}“ steht nicht auf der Seite.")
    for row in page["abzuege_gesamt"]:
        if parse_amount(row["betrag"]) is None or not _in_text(
            row["betrag"], compact, amount=True
        ):
            problems.append(f"Summenabzug „{row['betrag']}“ steht nicht auf der Seite.")
    return problems


ExtractFn = Callable[..., dict]


def extract_pages(
    pages: list[str],
    *,
    extract: ExtractFn,
    company: str,
) -> dict:
    """Liest alle Seiten aus (mit einem zweiten Versuch je fehlerhafter Seite).

    ``extract(page_text, feedback=...)`` liefert die Rohantwort der KI. Ergebnis ist die
    gespeicherte Abschrift: je Seite Text, JSON und verbliebene Fehler.
    """
    result_pages = []
    for number, text in enumerate(pages, start=1):
        feedback = ""
        page: dict = {}
        problems: list[str] = []
        for _attempt in range(2):
            try:
                page = _clean_page(extract(text, feedback=feedback, company=company))
            except ReceiptLLMError as exc:
                raise PaymentAdviceError(f"Seite {number}: {exc}") from exc
            problems = check_page(page, text)
            if not problems:
                break
            feedback = "\n".join(f"- {problem}" for problem in problems)
        page["positionen"] = [p for p in page.get("positionen", []) if not _is_subtotal(p)]
        result_pages.append({"page": number, "text": text, "data": page, "problems": problems})
    return {"pages": result_pages}


# ---------------------------------------------------------------------------
# Zuordnen und erklären
# ---------------------------------------------------------------------------


def categorize(text: str) -> str:
    """Kategorie eines Abzugs aus Bezeichnung oder Text."""
    lowered = (text or "").casefold()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    if re.search(r"\bdv\b", lowered):
        return CAT_DELKREDERE
    return CAT_UNCLEAR


def skonto_rate(base: Decimal, difference: Decimal) -> Decimal | None:
    """Glatter Skontosatz, wenn ``difference`` auf den Cent ``base × Satz`` ist."""
    if base <= 0 or difference <= 0:
        return None
    for rate in SKONTO_RATES:
        expected = (base * rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
        if abs(expected - difference) <= _CENT:
            return rate
    return None


@dataclass(slots=True)
class _Reason:
    light: str
    text: str


def _merge_header(pages: list[dict]) -> dict:
    header = {name: "" for name in _HEADER_FIELDS}
    for page in pages:
        for name in _HEADER_FIELDS:
            value = page["data"].get(name) or ""
            if not value:
                continue
            # Den Gesamtbetrag nennt meist die letzte Seite, die übrigen Felder die erste.
            if name == "zahlbetrag_gesamt" or not header[name]:
                header[name] = value
    return header


def _open_receivables(session: Session, company_id: int) -> list[OpenItem]:
    return list(
        session.execute(
            select(OpenItem)
            .where(OpenItem.company_id == company_id, OpenItem.item_type == "receivable")
            .order_by(OpenItem.id)
        ).scalars()
    )


def _reference_index(items: list[OpenItem]) -> dict[str, list[OpenItem]]:
    index: dict[str, list[OpenItem]] = {}
    for item in items:
        key = normalize_reference(item.reference)
        if key:
            index.setdefault(key, []).append(item)
    return index


def _lookup(index: dict[str, list[OpenItem]], raw: str) -> list[OpenItem]:
    key = normalize_reference(raw)
    if len(key) < 3:
        return []
    found = index.get(key, [])
    if not found and key.isdigit():
        # Führende Nullen schreibt mancher Kunde weg oder dazu.
        stripped = key.lstrip("0")
        found = [
            item
            for ref, items in index.items()
            if ref.isdigit() and ref.lstrip("0") == stripped
            for item in items
        ]
    return found


def _guess_partner(session: Session, company_id: int, names: list[str]) -> BusinessPartner | None:
    """Partner zum Zahler über den Namen, nur bei genau einem Treffer."""
    candidates = [n.casefold() for n in names if n and len(n) >= 3]
    if not candidates:
        return None
    partners = list(
        session.execute(
            select(BusinessPartner).where(
                BusinessPartner.company_id == company_id,
                BusinessPartner.is_active.is_(True),
            )
        ).scalars()
    )
    hits = [
        partner
        for partner in partners
        if partner.name
        and len(partner.name) >= 3
        and any(
            partner.name.casefold() in name or name in partner.name.casefold()
            for name in candidates
        )
    ]
    return hits[0] if len(hits) == 1 else None


def _abs(value: Decimal | None) -> Decimal | None:
    return None if value is None else abs(value)


def evaluate_advice(session: Session, advice: PaymentAdvice) -> dict:
    """Ordnet die Abschrift den offenen Posten zu und bewertet sie (Ampel, Hinweise).

    Setzt Kopfdaten, Partner, Ampel und ``result`` am Avis; committet nicht.
    """
    pages = (advice.extraction or {}).get("pages") or []
    header = _merge_header(pages)
    reasons: list[_Reason] = []
    for page in pages:
        for problem in page.get("problems") or []:
            reasons.append(_Reason(RED, f"Seite {page['page']}: {problem}"))

    advice.payer = (header["zahler"] or None) and header["zahler"][:255]
    advice.advice_number = (header["avis_nummer"] or None) and header["avis_nummer"][:120]
    advice.payment_date = parse_date(header["zahldatum"])
    currency = re.sub(r"[^A-Z]", "", header["waehrung"].upper())[:3]
    advice.currency_code = currency if len(currency) == 3 else None
    total = _abs(parse_amount(header["zahlbetrag_gesamt"]))
    advice.total_amount = total
    if total is None:
        reasons.append(_Reason(RED, "Der Gesamtbetrag des Avis fehlt."))

    items = _open_receivables(session, advice.company_id)
    index = _reference_index(items)
    used: set[int] = set()

    raw_positions = [
        (page["page"], position)
        for page in pages
        for position in (page.get("data") or {}).get("positionen", [])
    ]
    lines: list[dict] = []
    # 1. Durchgang: Zuordnung über die Belegnummer.
    for page_number, position in raw_positions:
        line = {
            "page": page_number,
            "kind": position["art"],
            "reference": position["belegnr"],
            "customer_reference": position["kunden_belegnr"],
            "date": position["datum"],
            "gross": _fmt(_abs(parse_amount(position["rechnungsbetrag"]))),
            "discount": _fmt(_abs(parse_amount(position["skonto"]))),
            "delcredere": _fmt(_abs(parse_amount(position["delkredere"]))),
            "other_deduction": _fmt(_abs(parse_amount(position["sonstiger_abzug"]))),
            "paid": _fmt(_abs(parse_amount(position["zahlbetrag"]))),
            "text": position["text"][:300],
            "open_item_id": None,
            "open_item_reference": None,
            "open_amount": None,
            "match": None,
            "settle_amount": None,
            "components": [],
            "light": GREEN,
            "notes": [],
        }
        lines.append(line)
        if position["art"] not in (KIND_INVOICE, KIND_CREDIT_NOTE):
            continue
        for raw in (position["belegnr"], position["kunden_belegnr"]):
            found = [item for item in _lookup(index, raw) if item.id not in used]
            open_found = [item for item in found if item.status == "open"]
            if open_found:
                _attach(line, open_found[0], "reference")
                used.add(open_found[0].id)
                break
            if found:
                line["notes"].append(
                    f"Posten {found[0].reference} ist bereits ausgeglichen."
                )
                line["light"] = RED
                break

    # Partner: der der zugeordneten Posten, sonst über den Namen des Zahlers.
    partner_ids = {
        session.get(OpenItem, line["open_item_id"]).partner_id
        for line in lines
        if line["open_item_id"]
    } - {None}
    partner = None
    if len(partner_ids) == 1:
        partner = session.get(BusinessPartner, next(iter(partner_ids)))
    elif not partner_ids:
        partner = _guess_partner(
            session, advice.company_id, [header["im_auftrag_von"], header["zahler"]]
        )
    advice.partner_id = partner.id if partner is not None else None

    # 2. Durchgang: Rechnungen ohne Treffer über Betrag (und Datum).
    open_items = [item for item in items if item.status == "open"]
    for line in lines:
        if line["kind"] != KIND_INVOICE or line["open_item_id"] or line["notes"]:
            continue
        item = _match_by_amount(line, open_items, used, partner)
        if item is not None:
            _attach(line, item, "amount")
            used.add(item.id)
            line["notes"].append(
                "Zugeordnet über Betrag und Datum: Das Avis nennt keine passende Belegnummer."
            )
            line["light"] = YELLOW

    # Abzüge mit der Nummer einer Rechnung dieses Avis gehören zu dieser Rechnung.
    invoice_by_item = {
        line["open_item_id"]: line
        for line in lines
        if line["kind"] == KIND_INVOICE and line["open_item_id"]
    }
    for line in lines:
        if line["kind"] not in (KIND_DEDUCTION, KIND_FEE):
            continue
        # Der Betrag einer Abzugszeile steht je nach Avis in einer beliebigen Betragsspalte.
        amount = Decimal(
            line["paid"] or line["gross"] or line["other_deduction"] or line["discount"]
            or line["delcredere"] or "0"
        )
        owner = None
        for raw in (line["reference"], line["customer_reference"]):
            hits = [i for i in _lookup(index, raw) if i.id in invoice_by_item]
            if hits:
                owner = invoice_by_item[hits[0].id]
                break
        category = categorize(f"{line['text']} {line['customer_reference']}")
        if category == CAT_UNCLEAR and line["kind"] == KIND_FEE:
            # Gebührenrechnung eines Finanzdienstleisters ohne weiteres Stichwort.
            category = CAT_FEE
        if owner is not None and category == CAT_UNCLEAR:
            base = Decimal(owner["open_amount"])
            if skonto_rate(base, amount) is not None:
                category = CAT_SKONTO
        line["components"].append(_component(category, amount, "Avis-Zeile"))
        if owner is not None:
            line["notes"].append(f"Abzug zur Rechnung {owner['open_item_reference']}.")
        if category == CAT_UNCLEAR:
            line["light"] = RED
            line["notes"].append("Abzug ohne erkennbaren Grund: beim Kunden nachfragen.")

    deduction_total = sum(
        (Decimal(c["amount"]) for line in lines if line["kind"] in (KIND_DEDUCTION, KIND_FEE)
         for c in line["components"]),
        Decimal("0.00"),
    )

    # Summenabzüge (nur bei der Gesamtsumme).
    summary_deductions = []
    for page in pages:
        for row in (page.get("data") or {}).get("abzuege_gesamt", []):
            amount = _abs(parse_amount(row["betrag"]))
            if amount is None or amount == 0:
                continue
            category = categorize(row["bezeichnung"])
            summary_deductions.append(
                {
                    "label": row["bezeichnung"][:200],
                    "amount": _fmt(amount),
                    "category": category,
                    "category_label": CATEGORY_LABELS[category],
                    "light": RED if category == CAT_UNCLEAR else GREEN,
                }
            )
    summary_total = sum(
        (Decimal(row["amount"]) for row in summary_deductions), Decimal("0.00")
    )
    if any(row["category"] == CAT_UNCLEAR for row in summary_deductions):
        reasons.append(_Reason(RED, "Ein Abzug auf die Summe hat keinen erkennbaren Grund."))

    # Rechnungen: Differenz zwischen offenem Betrag und Zahlung erklären.
    for line in lines:
        if line["kind"] == KIND_INVOICE:
            _explain_invoice(line, lines, invoice_by_item, advice.payment_date, open_items)
        elif line["kind"] == KIND_CREDIT_NOTE:
            line["light"] = max(line["light"], YELLOW, key=_LIGHT_RANK.get)
            line["notes"].append(
                "Gutschriften verrechnet der Ausgleich nicht: bitte von Hand ausgleichen."
            )

    # Summenprüfung: Zahlungen − Gutschriften − Abzüge = überwiesener Betrag.
    paid_total = sum(
        (Decimal(line["paid_amount"]) for line in lines if line.get("paid_amount")),
        Decimal("0.00"),
    )
    credit_total = sum(
        (
            Decimal(line["paid"] or line["gross"] or "0")
            for line in lines
            if line["kind"] == KIND_CREDIT_NOTE
        ),
        Decimal("0.00"),
    )
    positions_total = paid_total - credit_total - deduction_total - summary_total
    checks = {
        "invoices_paid": _fmt(paid_total),
        "credit_notes": _fmt(credit_total),
        "deductions": _fmt(deduction_total),
        "summary_deductions": _fmt(summary_total),
        "positions_total": _fmt(positions_total),
        "total_amount": _fmt(total),
        "difference": _fmt(positions_total - total) if total is not None else None,
        "ok": total is not None and abs(positions_total - total) <= _TOLERANCE,
    }
    if total is not None and not checks["ok"]:
        reasons.append(
            _Reason(
                RED,
                f"Die Positionen ergeben {_money(positions_total)}, überwiesen sind "
                f"{_money(total)}: Zeile vergessen, doppelt oder falsch ausgelesen.",
            )
        )
    if not lines:
        reasons.append(_Reason(RED, "Im Avis wurden keine Positionen erkannt."))

    # Belegnummern offener Posten, die im Avis stehen, aber in keiner Position.
    full_text = "\n".join(page.get("text") or "" for page in pages)
    matched_ids = {line["open_item_id"] for line in lines if line["open_item_id"]}
    for item in open_items:
        reference = (item.reference or "").strip()
        if (
            item.id not in matched_ids
            and len(normalize_reference(reference)) >= 5
            and re.search(r"\d", reference)
            and re.search(
                rf"(?<![0-9A-Za-z]){re.escape(reference)}(?![0-9A-Za-z])", full_text, re.I
            )
        ):
            reasons.append(
                _Reason(
                    RED,
                    f"Rechnung {item.reference} steht im Avis, fehlt aber in den Positionen.",
                )
            )

    for line in lines:
        if line["light"] != GREEN:
            label = line["reference"] or line["customer_reference"] or "ohne Nummer"
            reasons.append(_Reason(line["light"], f"{label}: " + " ".join(line["notes"])))

    light = max((reason.light for reason in reasons), key=_LIGHT_RANK.get, default=GREEN)
    advice.traffic_light = light

    booking = _booking_summary(lines, summary_deductions)
    result = {
        "lines": lines,
        "summary_deductions": summary_deductions,
        "checks": checks,
        "reasons": [{"light": r.light, "text": r.text} for r in reasons],
        "booking": booking,
        "bank_candidates": [
            _transaction_dict(transaction)
            for transaction in bank_candidates(session, advice)
        ],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    advice.result = result
    return result


def _attach(line: dict, item: OpenItem, method: str) -> None:
    line["open_item_id"] = item.id
    line["open_item_reference"] = item.reference
    line["open_amount"] = _fmt(item.open_amount)
    line["match"] = method


def _component(category: str, amount: Decimal, source: str) -> dict:
    return {
        "category": category,
        "label": CATEGORY_LABELS[category],
        "amount": _fmt(amount),
        "source": source,
    }


def _match_by_amount(
    line: dict,
    open_items: list[OpenItem],
    used: set[int],
    partner: BusinessPartner | None,
) -> OpenItem | None:
    """Rechnung ohne Nummer: Betrag auf den Cent; bei unbekanntem Partner auch das Datum."""
    amounts = {Decimal(value) for value in (line["gross"], line["paid"]) if value}
    if not amounts:
        return None
    line_date = parse_date(line["date"])
    candidates = [
        item
        for item in open_items
        if item.id not in used
        and not item.foreign_currency_code
        and item.open_amount in amounts
        and (partner is None or item.partner_id == partner.id)
    ]
    if partner is None or len(candidates) > 1:
        # Ohne Partner nie nur über den Betrag; bei mehreren Treffern entscheidet das Datum.
        tail = normalize_reference(line["customer_reference"])[-4:]
        candidates = [
            item
            for item in candidates
            if (line_date is not None and item.entry_date == line_date)
            or (len(tail) == 4 and normalize_reference(item.reference).endswith(tail))
        ]
    return candidates[0] if len(candidates) == 1 else None


def _explain_invoice(
    line: dict,
    lines: list[dict],
    invoice_by_item: dict[int, dict],
    payment_date: date | None,
    open_items: list[OpenItem],
) -> None:
    gross = Decimal(line["gross"]) if line["gross"] else None
    explicit = [
        (CAT_SKONTO, line["discount"]),
        (CAT_DELKREDERE, line["delcredere"]),
        (categorize(line["text"]) if line["other_deduction"] else CAT_UNCLEAR,
         line["other_deduction"]),
    ]
    explicit_total = sum((Decimal(v) for _c, v in explicit if v), Decimal("0.00"))
    if line["paid"]:
        paid = Decimal(line["paid"])
    elif gross is not None:
        paid = gross - explicit_total
    else:
        paid = None
    line["paid_amount"] = _fmt(paid)
    if not line["open_item_id"]:
        if not line["notes"]:
            line["notes"].append(
                "Kein offener Posten gefunden: schon ausgeglichen, Nummer falsch oder Posten fehlt."
            )
        line["light"] = RED
        return
    open_amount = Decimal(line["open_amount"])
    line["settle_amount"] = _fmt(open_amount)
    for category, value in explicit:
        if value and Decimal(value) > 0:
            line["components"].append(_component(category, Decimal(value), "Avis-Spalte"))
            if category == CAT_UNCLEAR:
                line["light"] = RED
                line["notes"].append("Abzug in der Zeile ohne erkennbaren Grund.")
    # Abzugszeilen zu dieser Rechnung (Minuszeile mit derselben Nummer) zählen mit.
    related = sum(
        (
            Decimal(c["amount"])
            for other in lines
            if other["kind"] in (KIND_DEDUCTION, KIND_FEE)
            and any(
                normalize_reference(raw) == normalize_reference(line["open_item_reference"])
                for raw in (other["reference"], other["customer_reference"])
                if raw
            )
            for c in other["components"]
        ),
        Decimal("0.00"),
    )
    if paid is None:
        line["light"] = RED
        line["notes"].append("Kein Zahlbetrag erkennbar.")
        return
    residual = open_amount - paid - explicit_total - related
    if residual == 0:
        pass
    elif abs(residual) <= _CENT and (explicit_total or related):
        # Rundungscent zwischen den Abzügen im Avis und dem Posten: zum Skonto.
        line["components"].append(_component(CAT_SKONTO, residual, "Rundung"))
    elif residual > 0:
        rate = skonto_rate(open_amount, residual)
        if rate is not None:
            line["components"].append(
                _component(CAT_SKONTO, residual, f"{str(rate).replace('.', ',')} % vom Posten")
            )
            line["notes"].append(
                f"Skonto {str(rate).replace('.', ',')} % ist im Zahlbetrag schon abgezogen."
            )
        else:
            line["light"] = RED
            line["notes"].append(
                f"Differenz {_money(residual)} zum offenen Betrag {_money(open_amount)} "
                "ist nicht erklärt (Teilzahlung oder anderer Abzug)."
            )
    else:
        line["light"] = RED
        line["notes"].append(
            f"Der Kunde zahlt {_money(-residual)} mehr als offen ist ({_money(open_amount)})."
        )
    if gross is not None and abs(gross - open_amount) > _CENT and not line["notes"]:
        line["notes"].append(
            f"Rechnungsbetrag im Avis {_money(gross)}, offen sind {_money(open_amount)}."
        )
    has_skonto = any(c["category"] == CAT_SKONTO for c in line["components"]) or any(
        c["category"] == CAT_SKONTO
        for other in lines
        if other["kind"] in (KIND_DEDUCTION, KIND_FEE)
        for c in other["components"]
        if any(
            normalize_reference(raw) == normalize_reference(line["open_item_reference"])
            for raw in (other["reference"], other["customer_reference"])
            if raw
        )
    )
    item = next((i for i in open_items if i.id == line["open_item_id"]), None)
    if has_skonto and payment_date is not None and item is not None:
        days = (payment_date - item.entry_date).days
        if days > SKONTO_DAYS:
            line["notes"].append(
                f"Skonto nach {days} Tagen: Frist mit den Zahlungsbedingungen abgleichen."
            )
            line["light"] = max(line["light"], YELLOW, key=_LIGHT_RANK.get)


def _booking_summary(lines: list[dict], summary_deductions: list[dict]) -> dict:
    """Beträge der Ausgleichsbuchung: Posten, Skonto, übrige Abzüge je Kategorie."""
    settled = sum(
        (Decimal(line["settle_amount"]) for line in lines if line.get("settle_amount")),
        Decimal("0.00"),
    )
    by_category: dict[str, Decimal] = {}
    for line in lines:
        for component in line["components"]:
            by_category[component["category"]] = by_category.get(
                component["category"], Decimal("0.00")
            ) + Decimal(component["amount"])
    for row in summary_deductions:
        by_category[row["category"]] = by_category.get(row["category"], Decimal("0.00")) + (
            Decimal(row["amount"])
        )
    discount = by_category.get(CAT_SKONTO, Decimal("0.00"))
    other = sum(
        (amount for category, amount in by_category.items() if category != CAT_SKONTO),
        Decimal("0.00"),
    )
    return {
        "settled_total": _fmt(settled),
        "discount_total": _fmt(discount),
        "other_deductions_total": _fmt(other),
        "by_category": [
            {"category": c, "label": CATEGORY_LABELS[c], "amount": _fmt(a)}
            for c, a in sorted(by_category.items())
        ],
    }


# ---------------------------------------------------------------------------
# Bankumsatz
# ---------------------------------------------------------------------------


def bank_candidates(
    session: Session, advice: PaymentAdvice, limit: int = 5
) -> list[BankTransaction]:
    """Offene, ungebuchte Zahlungseingänge über den Avis-Betrag, nach Nähe zum Zahldatum."""
    if advice.total_amount is None or advice.total_amount <= 0:
        return []
    transactions = list(
        session.execute(
            select(BankTransaction).where(
                BankTransaction.company_id == advice.company_id,
                BankTransaction.status == "open",
                BankTransaction.journal_entry_id.is_(None),
                BankTransaction.amount == advice.total_amount,
            )
        ).scalars()
    )
    anchor = advice.payment_date or date.today()
    transactions.sort(key=lambda t: (abs((t.booking_date - anchor).days), t.id))
    return transactions[:limit]


def _transaction_dict(transaction: BankTransaction) -> dict:
    return {
        "id": transaction.id,
        "booking_date": transaction.booking_date.isoformat(),
        "amount": _fmt(transaction.amount),
        "currency_code": transaction.currency_code,
        "counterparty": transaction.counterparty,
        "purpose": (transaction.purpose or "")[:200],
    }


# ---------------------------------------------------------------------------
# Auftrag: anlegen, im Hintergrund auslesen, neu zuordnen, löschen
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _start_thread(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, name="payment-advice", daemon=True)
    thread.start()
    return thread


def expire_stale(session: Session, advices: list[PaymentAdvice]) -> None:
    now = datetime.now(timezone.utc)
    changed = False
    for advice in advices:
        if advice.status == STATUS_RUNNING and now - _utc(advice.created_at) > JOB_STALE_AFTER:
            advice.status = STATUS_FAILED
            advice.error = STALE_ERROR
            advice.finished_at = now
            changed = True
    if changed:
        session.commit()


def list_advices(session: Session, company_id: int, limit: int = 50) -> list[PaymentAdvice]:
    advices = list(
        session.execute(
            select(PaymentAdvice)
            .where(PaymentAdvice.company_id == company_id)
            .order_by(PaymentAdvice.id.desc())
            .limit(max(1, min(limit, 200)))
        ).scalars()
    )
    expire_stale(session, advices)
    return advices


def get_advice(session: Session, company_id: int, advice_id: int) -> PaymentAdvice:
    advice = session.get(PaymentAdvice, advice_id)
    if advice is None or advice.company_id != company_id:
        raise PaymentAdviceError("Zahlungsavis nicht gefunden.")
    expire_stale(session, [advice])
    return advice


def start_advice(
    *,
    session: Session,
    company_id: int,
    document_id: int,
    changed_by: str,
    analysis_options: dict[str, object],
) -> PaymentAdvice:
    """Legt das Avis zu einem Beleg an und startet das Auslesen im Hintergrund.

    ``analysis_options`` kommt aus ``receipt_analysis_options`` (KI-Zugang des Benutzers).
    """
    company = session.get(Company, company_id)
    if company is None:
        raise PaymentAdviceError("Gesellschaft nicht gefunden.")
    document = session.get(Document, document_id)
    if document is None or document.company_id != company.id:
        raise PaymentAdviceError("Beleg nicht gefunden.")
    if not document.is_current:
        raise PaymentAdviceError("Der Beleg wurde durch eine neuere Version ersetzt.")
    if not analysis_options.get("llm_endpoint"):
        raise PaymentAdviceError(
            "Für Zahlungsavise wird ein KI-Zugang benötigt (Verwaltung: KI-Zugang des "
            "Benutzers oder RECEIPT_LLM_ENDPOINT_URL)."
        )
    existing = list(
        session.execute(
            select(PaymentAdvice).where(
                PaymentAdvice.document_id == document.id,
                PaymentAdvice.status.in_([STATUS_RUNNING, STATUS_APPLIED, STATUS_READY]),
            )
        ).scalars()
    )
    expire_stale(session, existing)
    for advice in existing:
        if advice.status == STATUS_RUNNING:
            raise PaymentAdviceError("Dieses Avis wird bereits ausgelesen.")
        if advice.status == STATUS_APPLIED and not _entry_reversed(advice):
            raise PaymentAdviceError("Dieses Avis ist bereits ausgeglichen.")
        if advice.status == STATUS_READY:
            raise PaymentAdviceError(
                f"Zu diesem Beleg gibt es schon das Avis {advice.id}: bitte dort neu zuordnen "
                "oder es löschen."
            )

    advice = PaymentAdvice(
        tenant_id=company.tenant_id,
        company_id=company.id,
        document_id=document.id,
        status=STATUS_RUNNING,
        created_by=changed_by,
    )
    session.add(advice)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="payment_advice",
        entity_id=str(advice.id),
        action="created",
        changed_by=changed_by,
        payload={"document_id": document.id, "file_name": document.file_name},
    )
    session.commit()

    app = current_app._get_current_object()
    session_factory = app.extensions["db_session_factory"]
    advice_id = advice.id

    def worker() -> None:
        with app.app_context(), llm_timeout_floor(300):
            run_extraction(session_factory, advice_id, analysis_options)

    _start_thread(worker)
    return advice


def run_extraction(session_factory, advice_id: int, options: dict[str, object]) -> None:
    """Auslesen und Zuordnen eines Avis (läuft im Hintergrund-Thread)."""
    with session_factory() as session:
        advice = session.get(PaymentAdvice, advice_id)
        if advice is None:
            return
        try:
            document = advice.document
            company = session.get(Company, advice.company_id)
            try:
                file_bytes = Path(document.storage_key).read_bytes()
            except OSError as exc:
                raise PaymentAdviceError(
                    f"Die Datei {document.file_name} ist nicht lesbar."
                ) from exc
            pages = advice_pages(
                file_bytes=file_bytes,
                mime_type=document.mime_type,
                file_name=document.file_name,
                ocr_endpoint=options.get("ocr_endpoint"),
                ocr_model=str(options.get("ocr_model") or "gpt-4.1-mini"),
                ocr_api_key=options.get("ocr_api_key"),
            )

            def extract(text: str, *, feedback: str, company: str) -> dict:
                return call_extraction_llm(
                    text,
                    endpoint_url=str(options["llm_endpoint"]),
                    model=str(options.get("llm_model") or "gpt-4.1-mini"),
                    api_key=options.get("llm_api_key"),
                    company=company,
                    feedback=feedback,
                )

            advice.extraction = extract_pages(pages, extract=extract, company=company.name)
            evaluate_advice(session, advice)
        except PaymentAdviceError as exc:
            session.rollback()
            _fail(session, advice_id, str(exc))
            return
        except Exception:  # noqa: BLE001 – der Fehler gehört an das Avis, nicht ins Nichts
            logger.exception("Zahlungsavis %s: Fehler beim Auslesen", advice_id)
            session.rollback()
            _fail(session, advice_id, "Interner Fehler beim Auslesen des Avis.")
            return
        advice.status = STATUS_READY
        advice.error = None
        advice.finished_at = datetime.now(timezone.utc)
        session.commit()


def _fail(session: Session, advice_id: int, error: str) -> None:
    advice = session.get(PaymentAdvice, advice_id)
    if advice is None:
        return
    advice.status = STATUS_FAILED
    advice.error = error
    advice.finished_at = datetime.now(timezone.utc)
    session.commit()


def rematch_advice(*, session: Session, advice: PaymentAdvice, changed_by: str) -> PaymentAdvice:
    """Ordnet ein ausgelesenes Avis erneut zu (neue OPOS-Lage, neue Bankumsätze)."""
    if advice.status != STATUS_READY:
        raise PaymentAdviceError("Nur ausgelesene, nicht ausgeglichene Avise lassen sich zuordnen.")
    evaluate_advice(session, advice)
    session.commit()
    return advice


def delete_advice(*, session: Session, advice: PaymentAdvice, changed_by: str) -> None:
    """Löscht ein Avis (nicht den Beleg). Ausgeglichene nur nach Storno der Buchung."""
    if advice.status == STATUS_RUNNING:
        raise PaymentAdviceError("Das Avis wird gerade ausgelesen.")
    if advice.status == STATUS_APPLIED and not _entry_reversed(advice):
        raise PaymentAdviceError(
            "Das Avis ist ausgeglichen. Bitte zuerst die Ausgleichsbuchung stornieren."
        )
    log_audit_event(
        session=session,
        tenant_id=advice.tenant_id,
        company_id=advice.company_id,
        entity_type="payment_advice",
        entity_id=str(advice.id),
        action="deleted",
        changed_by=changed_by,
        payload={"document_id": advice.document_id, "status": advice.status},
    )
    session.delete(advice)
    session.commit()


def _entry_reversed(advice: PaymentAdvice) -> bool:
    entry = advice.journal_entry
    return entry is None or entry.reversed_by is not None


# ---------------------------------------------------------------------------
# Ausgleich
# ---------------------------------------------------------------------------


def _account(session: Session, company_id: int, account_id: int | None, label: str) -> Account:
    account = session.get(Account, account_id) if account_id else None
    if account is None or account.company_id != company_id:
        raise PaymentAdviceError(f"Bitte ein {label} wählen.")
    if not account.is_active:
        raise PaymentAdviceError(f"Das {label} {account.code} ist inaktiv.")
    return account


def _tax_code(session: Session, company_id: int, tax_code_id: int | None) -> TaxCode | None:
    if not tax_code_id:
        return None
    tax_code = session.get(TaxCode, tax_code_id)
    if tax_code is None or tax_code.company_id != company_id:
        raise PaymentAdviceError("Steuercode nicht gefunden.")
    if not tax_code.is_active:
        raise PaymentAdviceError("Inaktive Steuercodes dürfen nicht verwendet werden.")
    return tax_code


def _deduction_lines(
    *, account: Account, tax_code: TaxCode | None, gross: Decimal, description: str
) -> list[JournalLineInput]:
    """Abzug im Soll; mit Steuercode netto auf das Konto, die Steuer als eigene Zeile."""
    if tax_code is None or tax_code.rate <= 0:
        return [
            JournalLineInput(
                account_id=account.id,
                debit_amount=gross,
                description=description,
                tax_code_id=tax_code.id if tax_code else None,
            )
        ]
    if tax_code.vat_account_id is None:
        raise PaymentAdviceError(f"Steuercode {tax_code.code} hat kein Steuerkonto hinterlegt.")
    net, tax = net_from_gross(gross, tax_code.rate)
    lines = [
        JournalLineInput(
            account_id=account.id,
            debit_amount=net,
            description=description,
            tax_code_id=tax_code.id,
        )
    ]
    if tax > 0:
        lines.append(
            JournalLineInput(
                account_id=tax_code.vat_account_id,
                debit_amount=tax,
                description=f"{tax_code.code} {tax_code.rate}% auf {net} (aus Brutto {gross})",
                tax_code_id=tax_code.id,
            )
        )
    return lines


def apply_advice(
    *,
    session: Session,
    advice: PaymentAdvice,
    bank_transaction_id: int,
    changed_by: str,
    discount_account_id: int | None = None,
    discount_tax_code_id: int | None = None,
    deduction_account_id: int | None = None,
    deduction_tax_code_id: int | None = None,
) -> PaymentAdvice:
    """Bucht den Bankumsatz gegen die zugeordneten Posten und die Abzüge und gleicht aus.

    Eine Buchung: Bank im Soll (überwiesener Betrag), je Posten das Sammelkonto im Haben
    (offener Betrag, mit Partner), Skonto und übrige Abzüge im Soll. Danach werden die Posten
    mit dieser Buchung ausgeglichen; ein Storno lässt sie wieder aufleben. Rote Avise und
    Avise mit Gutschriften gleicht das Programm nicht aus.
    """
    if advice.status == STATUS_APPLIED and not _entry_reversed(advice):
        raise PaymentAdviceError("Das Avis ist bereits ausgeglichen.")
    if advice.status not in (STATUS_READY, STATUS_APPLIED):
        raise PaymentAdviceError("Das Avis ist noch nicht ausgelesen.")
    company_id = advice.company_id
    # Sperrreihenfolge: Gesellschaft (Istversteuerung), Umsatz, Posten – alles vor dem
    # ersten Eintrag ins Audit-Protokoll.
    lock_cash_basis_company(session, company_id)
    transaction = session.execute(
        select(BankTransaction)
        .where(BankTransaction.id == bank_transaction_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if transaction is None or transaction.company_id != company_id:
        raise PaymentAdviceError("Bankumsatz nicht gefunden.")
    if transaction.status != "open" or transaction.journal_entry_id is not None:
        raise PaymentAdviceError("Der Bankumsatz ist bereits zugeordnet oder gebucht.")
    if transaction.amount <= 0:
        raise PaymentAdviceError("Ein Avis gleicht nur Zahlungseingänge aus.")
    company = session.get(Company, company_id)
    if transaction.currency_code and transaction.currency_code != company.currency_code:
        raise PaymentAdviceError(
            f"Der Umsatz läuft in {transaction.currency_code}: Avise in Fremdwährung bitte "
            "von Hand ausgleichen."
        )

    result = evaluate_advice(session, advice)
    lines = result["lines"]
    if advice.traffic_light == RED:
        raise PaymentAdviceError(
            "Das Avis ist rot: bitte die Hinweise klären, dann neu zuordnen."
        )
    if any(line["kind"] == KIND_CREDIT_NOTE for line in lines):
        raise PaymentAdviceError(
            "Das Avis verrechnet Gutschriften: bitte von Hand ausgleichen."
        )
    if advice.total_amount != transaction.amount:
        raise PaymentAdviceError(
            f"Der Bankumsatz über {_money(transaction.amount)} passt nicht zum Avis über "
            f"{_money(advice.total_amount or Decimal('0'))}."
        )
    item_ids = sorted(line["open_item_id"] for line in lines if line.get("settle_amount"))
    if not item_ids:
        raise PaymentAdviceError("Das Avis ordnet keinen offenen Posten zu.")
    items = {
        item.id: item
        for item in session.execute(
            select(OpenItem)
            .where(OpenItem.id.in_(item_ids))
            .order_by(OpenItem.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalars()
    }
    for line in lines:
        if not line.get("settle_amount"):
            continue
        item = items.get(line["open_item_id"])
        if item is None or item.status != "open" or item.open_amount != Decimal(
            line["settle_amount"]
        ):
            raise PaymentAdviceError(
                f"Posten {line['open_item_reference']} hat sich geändert: bitte neu zuordnen."
            )
        if item.foreign_currency_code:
            raise PaymentAdviceError(
                f"Posten {item.reference} läuft in {item.foreign_currency_code}: bitte von "
                "Hand ausgleichen."
            )

    booking = result["booking"]
    discount = Decimal(booking["discount_total"])
    other = Decimal(booking["other_deductions_total"])
    settled = Decimal(booking["settled_total"])
    if transaction.amount + discount + other != settled:
        raise PaymentAdviceError(
            f"Die Buchung geht nicht auf: Bank {_money(transaction.amount)} + Abzüge "
            f"{_money(discount + other)} ≠ Posten {_money(settled)}."
        )

    label = advice.advice_number or advice.document.file_name
    description = f"Zahlungsavis {label}"[:500]
    journal_lines = [
        JournalLineInput(
            account_id=transaction.bank_account_id,
            debit_amount=transaction.amount,
            description=description,
        )
    ]
    for line in lines:
        if not line.get("settle_amount"):
            continue
        item = items[line["open_item_id"]]
        journal_lines.append(
            JournalLineInput(
                account_id=item.account_id,
                credit_amount=item.open_amount,
                description=f"Ausgleich {item.reference}"[:500],
                partner_id=item.partner_id,
            )
        )
    if discount > 0:
        journal_lines += _deduction_lines(
            account=_account(session, company_id, discount_account_id, "Skontokonto"),
            tax_code=_tax_code(session, company_id, discount_tax_code_id),
            gross=discount,
            description=f"Skonto lt. {description}"[:500],
        )
    if other > 0:
        journal_lines += _deduction_lines(
            account=_account(session, company_id, deduction_account_id, "Konto für Abzüge"),
            tax_code=_tax_code(session, company_id, deduction_tax_code_id),
            gross=other,
            description=f"Abzüge lt. {description}"[:500],
        )
    partner_ids = {item.partner_id for item in items.values()} - {None}
    try:
        entry = create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company_id,
                entry_date=transaction.booking_date,
                description=description,
                status="posted",
                changed_by=changed_by,
                lines=journal_lines,
                expand_tax_lines=False,
                partner_id=next(iter(partner_ids)) if len(partner_ids) == 1 else None,
            ),
            commit=False,
        )
        transaction.journal_entry_id = entry.id
        transaction.status = "booked"
        log_audit_event(
            session=session,
            tenant_id=transaction.tenant_id,
            company_id=company_id,
            entity_type="bank_transaction",
            entity_id=str(transaction.id),
            action="booked",
            changed_by=changed_by,
            payload={
                "journal_entry_id": entry.id,
                "posting_number": entry.posting_number,
                "payment_advice_id": advice.id,
            },
        )
        for item_id in item_ids:
            settle_open_item(
                session=session,
                open_item_id=item_id,
                changed_by=changed_by,
                amount=items[item_id].open_amount,
                journal_entry_id=entry.id,
                settlement_date=transaction.booking_date,
                commit=False,
            )
    except (OpenItemError, JournalEntryCreationError, JournalEntryValidationError) as exc:
        session.rollback()
        raise PaymentAdviceError(str(exc)) from exc
    except Exception:
        session.rollback()
        raise

    document = advice.document
    if document.journal_entry_id is None:
        document.journal_entry_id = entry.id
        log_audit_event(
            session=session,
            tenant_id=document.tenant_id,
            company_id=company_id,
            entity_type="document",
            entity_id=str(document.id),
            action="linked",
            changed_by=changed_by,
            payload={"journal_entry_id": entry.id, "payment_advice_id": advice.id},
        )
    advice.status = STATUS_APPLIED
    advice.bank_transaction_id = transaction.id
    advice.journal_entry_id = entry.id
    advice.applied_by = changed_by
    advice.applied_at = datetime.now(timezone.utc)
    log_audit_event(
        session=session,
        tenant_id=advice.tenant_id,
        company_id=company_id,
        entity_type="payment_advice",
        entity_id=str(advice.id),
        action="applied",
        changed_by=changed_by,
        payload={
            "journal_entry_id": entry.id,
            "bank_transaction_id": transaction.id,
            "open_item_ids": item_ids,
            "discount": _fmt(discount),
            "other_deductions": _fmt(other),
        },
    )
    session.commit()
    session.refresh(advice)
    return advice


# ---------------------------------------------------------------------------
# Darstellung (UI, REST, MCP)
# ---------------------------------------------------------------------------


def advice_dict(advice: PaymentAdvice, *, with_extraction: bool = False) -> dict:
    entry = advice.journal_entry
    data = {
        "id": advice.id,
        "company_id": advice.company_id,
        "document_id": advice.document_id,
        "file_name": advice.document.file_name if advice.document is not None else None,
        "status": advice.status,
        "error": advice.error,
        "traffic_light": advice.traffic_light,
        "traffic_light_label": LIGHT_LABELS.get(advice.traffic_light or ""),
        "payer": advice.payer,
        "advice_number": advice.advice_number,
        "payment_date": advice.payment_date.isoformat() if advice.payment_date else None,
        "currency_code": advice.currency_code,
        "total_amount": _fmt(advice.total_amount),
        "partner_id": advice.partner_id,
        "partner_name": advice.partner.name if advice.partner is not None else None,
        "bank_transaction_id": advice.bank_transaction_id,
        "journal_entry_id": advice.journal_entry_id,
        "posting_number": entry.posting_number if entry is not None else None,
        "journal_entry_reversed": bool(entry is not None and entry.reversed_by is not None),
        "result": advice.result,
        "created_by": advice.created_by,
        "created_at": _utc(advice.created_at).isoformat(),
        "finished_at": _utc(advice.finished_at).isoformat() if advice.finished_at else None,
        "applied_by": advice.applied_by,
        "applied_at": _utc(advice.applied_at).isoformat() if advice.applied_at else None,
    }
    if with_extraction:
        data["extraction"] = advice.extraction
    return data
