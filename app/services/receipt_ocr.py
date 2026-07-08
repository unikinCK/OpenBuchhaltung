"""OCR-Pipeline für Belege: Text aus einem Beleg gewinnen und daraus einen
Buchungsvorschlag ableiten.

Die Pipeline besteht aus zwei klar getrennten Stufen:

1. **Textgewinnung** (:func:`extract_document_text`):
   * ``text/plain`` wird direkt dekodiert.
   * PDFs mit eingebetteter Textebene werden mit Bordmitteln (``zlib``) ausgelesen.
   * Bild-Belege (JPG/PNG) und Scan-PDFs ohne Textebene werden an einen optionalen
     externen OCR-Endpoint (OpenAI-``/responses``-kompatibel) geschickt. Ist keiner
     konfiguriert, meldet die Pipeline das verständlich zurück – der Upload selbst
     bleibt dadurch unberührt (analog zum bestehenden Beleg-LLM).

2. **Heuristische Analyse** (:func:`analyze_receipt_text`):
   Der Freitext wird namespace-frei nach den buchungsrelevanten Feldern durchsucht
   (Bruttobetrag, Nettobetrag, Steuerbetrag, Steuersatz, Rechnungsdatum,
   Rechnungsnummer, Lieferant) und – wo möglich – rechnerisch zu einem konsistenten
   Netto/Steuer/Brutto-Vorschlag vervollständigt. Diese Stufe ist vollständig
   deterministisch und ohne externe Abhängigkeiten testbar.
"""

from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_CENT = Decimal("0.01")


class ReceiptOCRError(ValueError):
    """Raised when a receipt cannot be turned into extractable text."""


@dataclass(slots=True)
class ReceiptExtraction:
    """Aus einem Beleg gewonnene Buchungsdaten (alle Felder optional)."""

    raw_text: str
    supplier: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    tax_rate: Decimal | None = None
    currency_code: str = "EUR"
    confidence: str = "niedrig"
    source: str = "text"  # "text", "pdf" oder "ocr-endpoint"
    warnings: list[str] = field(default_factory=list)
    # KI-Kontrolle: ob und mit welchem Ergebnis ein LLM gegengeprüft/ergänzt hat.
    llm_used: bool = False
    # None | "bestätigt" | "abweichung" | "ergänzt" | "nur_regelbasiert"
    control_status: str | None = None

    @property
    def has_booking_basis(self) -> bool:
        """Ob mindestens der Bruttobetrag für einen Vorschlag vorliegt."""
        return self.gross_amount is not None and self.gross_amount > 0


@dataclass(slots=True)
class LlmReceiptFields:
    """Von einem LLM strukturiert extrahierte Belegfelder (alle optional)."""

    supplier: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    tax_rate: Decimal | None = None
    currency_code: str | None = None


class ReceiptLLMError(ValueError):
    """Raised when the structured-extraction LLM cannot be used."""


# ---------------------------------------------------------------------------
# Stufe 1: Textgewinnung
# ---------------------------------------------------------------------------


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Best-effort-Extraktion der Textebene aus einem PDF ohne Fremdbibliotheken.

    Liest die (ggf. Flate-komprimierten) Content-Streams aus und sammelt die in
    ``(...)``-Literalen bzw. ``<...>``-Hex-Strings gezeigten Textstücke. Reicht für
    PDFs mit echter Textebene (maschinell erzeugte Rechnungen); echte Scans ohne
    Textebene liefern hier leeren Text und werden an den OCR-Endpoint delegiert.
    """
    chunks: list[str] = []
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf_bytes, re.DOTALL):
        content = raw
        try:
            content = zlib.decompress(raw)
        except zlib.error:
            # Unkomprimierter Stream (oder anderer Filter) – Rohbytes verwenden.
            content = raw
        chunks.append(_extract_text_operators(content))
    return "\n".join(chunk for chunk in chunks if chunk.strip())


def _extract_text_operators(content: bytes) -> str:
    """Extrahiert die gezeigten Strings eines PDF-Content-Streams."""
    text = _decode_text(content)
    lines: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "(":
            literal, index = _read_pdf_literal(text, index + 1)
            lines.append(literal)
            continue
        if char == "<" and index + 1 < length and text[index + 1] != "<":
            hex_string, index = _read_pdf_hex(text, index + 1)
            if hex_string:
                lines.append(hex_string)
            continue
        index += 1
    return "\n".join(part for part in lines if part.strip())


def _read_pdf_literal(text: str, start: int) -> tuple[str, int]:
    """Liest ein PDF-Literal ``(...)`` ab ``start`` inkl. Escapes und Verschachtelung."""
    out: list[str] = []
    depth = 1
    index = start
    length = len(text)
    escapes = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "(": "(",
        ")": ")",
        "\\": "\\",
    }
    while index < length:
        char = text[index]
        if char == "\\":
            nxt = text[index + 1] if index + 1 < length else ""
            out.append(escapes.get(nxt, nxt))
            index += 2
            continue
        if char == "(":
            depth += 1
            out.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                return "".join(out), index + 1
            out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out), index


def _read_pdf_hex(text: str, start: int) -> tuple[str, int]:
    end = text.find(">", start)
    if end == -1:
        return "", len(text)
    hex_digits = re.sub(r"\s", "", text[start:end])
    if len(hex_digits) % 2:
        hex_digits += "0"
    try:
        return bytes.fromhex(hex_digits).decode("latin-1"), end + 1
    except ValueError:
        return "", end + 1


def _ocr_via_endpoint(
    *, endpoint_url: str, model: str, file_bytes: bytes, mime_type: str, file_name: str
) -> str:
    """Schickt den Beleg an einen OpenAI-``/responses``-kompatiblen OCR-Endpoint.

    Erwartet als Antwort entweder ``output_text`` oder eine ``output``-Struktur mit
    ``input_text``/``output_text``-Blöcken; alle enthaltenen Textstücke werden
    zusammengeführt.
    """
    import base64

    encoded = base64.b64encode(file_bytes).decode("ascii")
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Du bist eine OCR-Engine für Belege einer deutschen "
                            "Buchhaltung. Gib ausschließlich den erkannten Belegtext "
                            "zurück, ohne Kommentar."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:{mime_type};base64,{encoded}",
                    },
                    {"type": "input_text", "text": f"Beleg: {file_name}"},
                ],
            },
        ],
        "metadata": {"source": "openbuchhaltung-receipt-ocr"},
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
        raise ReceiptOCRError(f"OCR-Endpoint antwortete mit HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ReceiptOCRError("OCR-Endpoint ist nicht erreichbar.") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptOCRError("OCR-Endpoint lieferte kein gültiges JSON.") from exc

    text = _collect_response_text(body)
    if not text.strip():
        raise ReceiptOCRError("OCR-Endpoint lieferte keinen Text.")
    return text


def _collect_response_text(body: dict) -> str:
    if isinstance(body.get("output_text"), str):
        return body["output_text"]
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") in {"output_text", "input_text"} and isinstance(
                node.get("text"), str
            ):
                parts.append(node["text"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(body)
    return "\n".join(parts)


def _classify(mime_type: str, file_name: str) -> str:
    extension = Path(file_name).suffix.lower()
    if mime_type == "application/pdf" or extension == ".pdf":
        return "pdf"
    if mime_type.startswith("image/") or extension in {".jpg", ".jpeg", ".png"}:
        return "image"
    if mime_type.startswith("text/") or extension == ".txt":
        return "text"
    return "unknown"


def extract_document_text(
    *,
    file_bytes: bytes,
    mime_type: str,
    file_name: str,
    ocr_endpoint: str | None = None,
    ocr_model: str = "gpt-4.1-mini",
) -> tuple[str, str]:
    """Gewinnt Text aus einem Beleg. Gibt ``(text, quelle)`` zurück.

    ``quelle`` ist ``"text"``, ``"pdf"`` oder ``"ocr-endpoint"``. Bild-/Scan-Belege
    ohne konfigurierten Endpoint lösen einen :class:`ReceiptOCRError` aus.
    """
    kind = _classify(mime_type, file_name)

    if kind == "text":
        return _decode_text(file_bytes), "text"

    if kind == "pdf":
        pdf_text = _extract_pdf_text(file_bytes)
        if len(pdf_text.strip()) >= 20:
            return pdf_text, "pdf"
        if ocr_endpoint:
            return (
                _ocr_via_endpoint(
                    endpoint_url=ocr_endpoint,
                    model=ocr_model,
                    file_bytes=file_bytes,
                    mime_type=mime_type or "application/pdf",
                    file_name=file_name,
                ),
                "ocr-endpoint",
            )
        raise ReceiptOCRError(
            "Das PDF enthält keine auslesbare Textebene. Für gescannte Belege bitte "
            "einen OCR-Endpoint (RECEIPT_OCR_ENDPOINT_URL) konfigurieren."
        )

    if kind == "image":
        if ocr_endpoint:
            return (
                _ocr_via_endpoint(
                    endpoint_url=ocr_endpoint,
                    model=ocr_model,
                    file_bytes=file_bytes,
                    mime_type=mime_type or "image/png",
                    file_name=file_name,
                ),
                "ocr-endpoint",
            )
        raise ReceiptOCRError(
            "Für Bild-Belege (JPG/PNG) wird ein OCR-Endpoint benötigt. Bitte "
            "RECEIPT_OCR_ENDPOINT_URL konfigurieren."
        )

    raise ReceiptOCRError(f"Belegtyp {mime_type!r} wird für OCR nicht unterstützt.")


# ---------------------------------------------------------------------------
# Stufe 2: Heuristische Analyse
# ---------------------------------------------------------------------------

# Betrag im deutschen (1.234,56) oder englischen (1,234.56 / 1234.56) Format.
_AMOUNT_RE = r"-?\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})|-?\d+(?:[.,]\d{1,2})?"

_GROSS_KEYWORDS = (
    "gesamtbetrag",
    "rechnungsbetrag",
    "zahlbetrag",
    "zu zahlen",
    "zu zahlender betrag",
    "gesamtsumme",
    "endbetrag",
    "bruttobetrag",
    "brutto",
    "gesamt brutto",
    "summe brutto",
    "total",
)
_NET_KEYWORDS = (
    "nettobetrag",
    "netto",
    "gesamt netto",
    "summe netto",
    "zwischensumme",
    "nettosumme",
)
_TAX_KEYWORDS = (
    "mehrwertsteuer",
    "umsatzsteuer",
    "mwst",
    "mwst.",
    "ust",
    "ust.",
    "vat",
    "steuerbetrag",
)


def _parse_amount(raw: str) -> Decimal | None:
    value = raw.strip()
    if not value:
        return None
    if "," in value and "." in value:
        # Deutsches Format: Punkt = Tausender, Komma = Dezimal.
        value = value.replace(".", "").replace(",", ".")
    elif "," in value:
        value = value.replace(".", "").replace(",", ".")
    value = value.replace(" ", "")
    try:
        return Decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _first_monetary_amount(window: str) -> Decimal | None:
    """Erster Geldbetrag im Fenster – Prozentangaben (z. B. ``19 %``) werden übersprungen."""
    for match in re.finditer(_AMOUNT_RE, window):
        trailing = window[match.end() : match.end() + 3].lstrip()
        if trailing.startswith("%"):
            continue
        amount = _parse_amount(match.group(0))
        if amount is not None:
            return amount
    return None


def _find_amount_for_keywords(text: str, keywords: tuple[str, ...]) -> Decimal | None:
    """Sucht den Betrag, der einem der Schlüsselwörter im Text am nächsten folgt."""
    best: Decimal | None = None
    best_pos = -1
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
            amount = _first_monetary_amount(text[match.end() : match.end() + 60])
            if amount is None:
                continue
            # Bevorzuge das zuletzt (weiter unten) genannte Vorkommen – auf Belegen
            # steht die maßgebliche Summe typischerweise am Ende.
            if match.start() > best_pos:
                best = amount
                best_pos = match.start()
    return best


def _find_tax_rate(text: str) -> Decimal | None:
    rates: list[Decimal] = []
    for match in re.finditer(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%", text):
        rate = _parse_amount(match.group(1))
        if rate is not None and Decimal("0") < rate <= Decimal("30"):
            rates.append(rate)
    if not rates:
        return None
    # Häufigsten Satz wählen (z. B. mehrere 19%-Angaben je Position).
    return max(set(rates), key=rates.count)


def _find_invoice_date(text: str) -> date | None:
    for match in re.finditer(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b", text):
        day, month, year = match.groups()
        year_int = int(year)
        if year_int < 100:
            year_int += 2000
        try:
            return date(year_int, int(month), int(day))
        except ValueError:
            continue
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
    return None


def _find_invoice_number(text: str) -> str | None:
    patterns = (
        r"rechnung(?:s)?[\s\-]*(?:nr|nummer)\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,30})",
        r"\brg[\s\-]*nr\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,30})",
        r"\binvoice\s*(?:no|number)\.?\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{1,30})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip(".-/")
    return None


def _find_supplier(text: str) -> str | None:
    skip = ("rechnung", "invoice", "beleg", "quittung", "datum", "seite")
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(token) for token in skip):
            continue
        if re.fullmatch(r"[\d\s.,:/€%-]+", stripped):
            continue
        return stripped[:120]
    return None


def _find_currency(text: str) -> str:
    if "€" in text or re.search(r"\bEUR\b", text, re.IGNORECASE):
        return "EUR"
    match = re.search(r"\b(USD|CHF|GBP)\b", text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return "EUR"


def _reconcile(
    net: Decimal | None,
    tax: Decimal | None,
    gross: Decimal | None,
    rate: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None, list[str]]:
    """Ergänzt fehlende Beträge rechnerisch und meldet Unstimmigkeiten."""
    warnings: list[str] = []

    if rate is None and net and tax and net > 0:
        rate = (tax / net * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    if gross is None and net is not None and tax is not None:
        gross = (net + tax).quantize(_CENT, rounding=ROUND_HALF_UP)

    if net is None and gross is not None and tax is not None:
        net = (gross - tax).quantize(_CENT, rounding=ROUND_HALF_UP)

    if net is None and gross is not None and rate is not None and rate > 0:
        net = (gross / (Decimal("1") + rate / Decimal("100"))).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )

    if tax is None and net is not None and rate is not None:
        tax = (net * rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)

    if tax is None and gross is not None and net is not None:
        tax = (gross - net).quantize(_CENT, rounding=ROUND_HALF_UP)

    if net is None and gross is not None and rate is None:
        # Nur Brutto bekannt und kein Steuerhinweis: ohne Steuer vorschlagen.
        net = gross
        tax = Decimal("0.00")
        rate = Decimal("0")
        warnings.append(
            "Kein Steuersatz erkannt – Vorschlag ohne Steuer. Bitte prüfen."
        )

    if net is not None and tax is not None and gross is not None:
        expected = (net + tax).quantize(_CENT, rounding=ROUND_HALF_UP)
        if abs(expected - gross) > _CENT:
            warnings.append(
                f"Netto ({net}) + Steuer ({tax}) ergibt {expected}, "
                f"Beleg nennt aber Brutto {gross}. Bitte prüfen."
            )

    return net, tax, gross, rate, warnings


def analyze_receipt_text(text: str) -> ReceiptExtraction:
    """Analysiert Belegfreitext und liefert einen Buchungsvorschlag."""
    result = ReceiptExtraction(raw_text=text)
    if not text or not text.strip():
        result.warnings.append("Kein Text zum Analysieren vorhanden.")
        return result

    gross = _find_amount_for_keywords(text, _GROSS_KEYWORDS)
    net = _find_amount_for_keywords(text, _NET_KEYWORDS)
    tax = _find_amount_for_keywords(text, _TAX_KEYWORDS)
    rate = _find_tax_rate(text)

    net, tax, gross, rate, warnings = _reconcile(net, tax, gross, rate)

    result.net_amount = net
    result.tax_amount = tax
    result.gross_amount = gross
    result.tax_rate = rate
    result.invoice_date = _find_invoice_date(text)
    result.invoice_number = _find_invoice_number(text)
    result.supplier = _find_supplier(text)
    result.currency_code = _find_currency(text)
    result.warnings.extend(warnings)

    result.confidence = _confidence(result)
    return result


def _confidence(result: ReceiptExtraction) -> str:
    if not result.has_booking_basis:
        return "niedrig"
    strong = (
        result.gross_amount is not None
        and result.net_amount is not None
        and result.tax_amount is not None
        and result.tax_rate is not None
        and not result.warnings
    )
    if strong and result.invoice_date is not None:
        return "hoch"
    if result.warnings:
        return "niedrig"
    return "mittel"


# ---------------------------------------------------------------------------
# Stufe 3: LLM als Unterstützung/Fallback und Kontrolle
# ---------------------------------------------------------------------------

_LLM_INSTRUCTION = (
    "Du extrahierst Buchungsdaten aus dem Text eines deutschen Belegs "
    "(Eingangsrechnung/Quittung). Antworte ausschließlich mit einem JSON-Objekt "
    "ohne weitere Erklärung und mit exakt diesen Feldern: "
    '{"supplier": string|null, "invoice_number": string|null, '
    '"invoice_date": "YYYY-MM-DD"|null, "net_amount": number|null, '
    '"tax_amount": number|null, "gross_amount": number|null, '
    '"tax_rate": number|null, "currency_code": string|null}. '
    "Beträge als Dezimalzahl mit Punkt, ohne Währungssymbol. Unbekannte Felder = null."
)


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        return _parse_amount(value)
    return None


def _to_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return _find_invoice_date(value)


def _parse_llm_json(text: str) -> dict:
    """Extrahiert das erste JSON-Objekt aus einer LLM-Antwort."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ReceiptLLMError("LLM-Antwort enthält kein JSON-Objekt.")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReceiptLLMError(f"LLM-JSON konnte nicht gelesen werden: {exc}") from exc
    if not isinstance(data, dict):
        raise ReceiptLLMError("LLM-JSON ist kein Objekt.")
    return data


def extract_receipt_fields_llm(
    text: str, *, endpoint_url: str, model: str
) -> LlmReceiptFields:
    """Lässt ein LLM die Belegfelder strukturiert (als JSON) extrahieren."""
    if not endpoint_url:
        raise ReceiptLLMError("LLM-Endpoint ist nicht konfiguriert.")
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": _LLM_INSTRUCTION}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": text[:8000]}],
            },
        ],
        "metadata": {"source": "openbuchhaltung-receipt-fields"},
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
        raise ReceiptLLMError(f"LLM-Endpoint antwortete mit HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ReceiptLLMError("LLM-Endpoint ist nicht erreichbar.") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptLLMError("LLM-Endpoint lieferte kein gültiges JSON.") from exc

    data = _parse_llm_json(_collect_response_text(body))
    return LlmReceiptFields(
        supplier=(data.get("supplier") or None),
        invoice_number=(str(data["invoice_number"]) if data.get("invoice_number") else None),
        invoice_date=_to_date(data.get("invoice_date")),
        net_amount=_to_decimal(data.get("net_amount")),
        tax_amount=_to_decimal(data.get("tax_amount")),
        gross_amount=_to_decimal(data.get("gross_amount")),
        tax_rate=_to_decimal(data.get("tax_rate")),
        currency_code=(data.get("currency_code") or None),
    )


def apply_llm_control(extraction: ReceiptExtraction, llm: LlmReceiptFields) -> None:
    """Führt LLM-Ergebnisse als Unterstützung ein und prüft sie als Kontrolle.

    * **Unterstützung/Fallback:** fehlende Felder (Text und Beträge) werden aus dem
      LLM ergänzt und anschließend rechnerisch konsolidiert.
    * **Kontrolle:** stimmt der regelbasierte Bruttobetrag mit dem LLM überein, gilt
      der Vorschlag als bestätigt; weicht er ab, wird gewarnt.
    """
    extraction.llm_used = True
    det_gross = extraction.gross_amount

    # Unterstützung: fehlende Textfelder ergänzen.
    if not extraction.supplier and llm.supplier:
        extraction.supplier = llm.supplier.strip()[:120]
    if not extraction.invoice_number and llm.invoice_number:
        extraction.invoice_number = llm.invoice_number.strip()
    if extraction.invoice_date is None and llm.invoice_date is not None:
        extraction.invoice_date = llm.invoice_date
    if llm.currency_code and llm.currency_code.strip():
        extraction.currency_code = llm.currency_code.strip().upper()

    # Unterstützung: fehlende Beträge aus dem LLM übernehmen, dann konsolidieren.
    net = extraction.net_amount if extraction.net_amount is not None else llm.net_amount
    tax = extraction.tax_amount if extraction.tax_amount is not None else llm.tax_amount
    gross = extraction.gross_amount if extraction.gross_amount is not None else llm.gross_amount
    rate = extraction.tax_rate if extraction.tax_rate is not None else llm.tax_rate
    net, tax, gross, rate, warns = _reconcile(net, tax, gross, rate)
    extraction.net_amount = net
    extraction.tax_amount = tax
    extraction.gross_amount = gross
    extraction.tax_rate = rate
    for warning in warns:
        if warning not in extraction.warnings:
            extraction.warnings.append(warning)

    # Kontrolle anhand des Bruttobetrags.
    if det_gross is not None and llm.gross_amount is not None:
        if abs(det_gross - llm.gross_amount) <= _CENT:
            extraction.control_status = "bestätigt"
        else:
            extraction.control_status = "abweichung"
            extraction.warnings.append(
                f"KI-Kontrolle: Bruttobetrag weicht ab (regelbasiert {det_gross}, "
                f"KI {llm.gross_amount}). Bitte prüfen."
            )
    elif det_gross is None and llm.gross_amount is not None:
        extraction.control_status = "ergänzt"
        if "+llm" not in extraction.source:
            extraction.source = f"{extraction.source}+llm"
    else:
        extraction.control_status = "nur_regelbasiert"

    extraction.confidence = _confidence_with_control(extraction)


def _confidence_with_control(extraction: ReceiptExtraction) -> str:
    base = _confidence(extraction)
    if extraction.control_status == "abweichung":
        return "niedrig"
    if (
        extraction.control_status == "bestätigt"
        and extraction.invoice_date is not None
        and not extraction.warnings
    ):
        return "hoch"
    return base


def analyze_document(
    *,
    file_bytes: bytes,
    mime_type: str,
    file_name: str,
    ocr_endpoint: str | None = None,
    ocr_model: str = "gpt-4.1-mini",
    llm_endpoint: str | None = None,
    llm_model: str = "gpt-4.1-mini",
) -> ReceiptExtraction:
    """Komplette Pipeline: Text gewinnen, regelbasiert analysieren und – falls ein
    ``llm_endpoint`` konfiguriert ist – per LLM ergänzen und gegenprüfen.

    LLM-Fehler blockieren die Pipeline nicht; sie werden als Warnung vermerkt, der
    regelbasierte Vorschlag bleibt erhalten.
    """
    text, source = extract_document_text(
        file_bytes=file_bytes,
        mime_type=mime_type,
        file_name=file_name,
        ocr_endpoint=ocr_endpoint,
        ocr_model=ocr_model,
    )
    extraction = analyze_receipt_text(text)
    extraction.source = source

    if llm_endpoint:
        try:
            llm_fields = extract_receipt_fields_llm(
                text, endpoint_url=llm_endpoint, model=llm_model
            )
            apply_llm_control(extraction, llm_fields)
        except ReceiptLLMError as exc:
            extraction.warnings.append(f"KI-Kontrolle nicht möglich: {exc}")

    return extraction
