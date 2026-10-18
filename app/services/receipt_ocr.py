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
import logging
import re
import zlib
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.services.llm_settings import llm_headers, llm_timeout

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")

# Steuerzeichen (inkl. NUL), die weder in Analyse-Text noch in DB-Feldern landen
# dürfen – PostgreSQL lehnt NUL-Bytes in Text-/JSON-Spalten mit einem DataError ab.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value: str) -> str:
    """Entfernt NUL-Bytes und sonstige Steuerzeichen (außer ``\\n``/``\\r``/``\\t``)."""
    return _CONTROL_CHARS_RE.sub("", value)


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
    supplier_vat_id: str | None = None
    # Aufteilung nach Steuersätzen: (Satz, Netto, Steuer), größter Nettobetrag zuerst.
    tax_lines: list[tuple[Decimal, Decimal, Decimal]] = field(default_factory=list)
    tip_amount: Decimal | None = None
    reverse_charge: bool = False
    description: str | None = None
    # Vom LLM aus dem Kontenplan gewählte Kontonummer (nur mit Kontenliste).
    suggested_account_code: str | None = None
    # KI-Kontrolle: ob und mit welchem Ergebnis ein LLM gegengeprüft/ergänzt hat.
    llm_used: bool = False
    # None | "bestätigt" | "korrigiert" | "abweichung" | "ergänzt" | "nur_regelbasiert"
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
    supplier_vat_id: str | None = None
    tax_lines: list[tuple[Decimal, Decimal, Decimal]] = field(default_factory=list)
    tip_amount: Decimal | None = None
    reverse_charge: bool = False
    description: str | None = None
    expense_account_code: str | None = None


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


def _pdf_text_is_usable(text: str) -> bool:
    """Ob extrahierter PDF-Text als echte Textebene taugt.

    Neben der Mindestlänge wird der Anteil an Steuer-/Ersatzzeichen geprüft:
    PDFs mit Font-Subsetting und eigenen Encodings (z. B. Rechnungen großer Versandhändler)
    liefern über die Bordmittel-Extraktion Zeichensalat voller NUL-Bytes, der
    sonst als "Text" durchginge und die Analyse (und die DB) vergiften würde.
    """
    stripped = text.strip()
    if len(stripped) < 20:
        return False
    garbled = sum(
        1
        for char in stripped
        if (ord(char) < 32 and char not in "\n\r\t") or char == "�"
    )
    return garbled / len(stripped) <= 0.1


def _extract_pdf_text_pypdf(pdf_bytes: bytes) -> str:
    """Extrahiert die Textebene mit ``pypdf`` (kennt Font-Encodings/ToUnicode-CMaps).

    Liefert bei fehlender Bibliothek oder nicht lesbarem PDF einen leeren String,
    damit die Bordmittel-Extraktion als Fallback greifen kann.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:  # pypdf wirft je nach Defekt sehr unterschiedliche Fehler.
        return ""
    return "\n".join(page for page in pages if page.strip())


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extrahiert die Textebene eines PDFs: erst ``pypdf``, dann Bordmittel.

    ``pypdf`` dekodiert auch PDFs mit Font-Subsetting/eigenen Encodings korrekt.
    Nur wenn es keinen brauchbaren Text liefert, greift die Bordmittel-Extraktion
    (:func:`_extract_pdf_text_builtin`); echte Scans ohne Textebene liefern in
    beiden Wegen leeren Text und werden an den OCR-Endpoint delegiert.
    """
    text = _extract_pdf_text_pypdf(pdf_bytes)
    if _pdf_text_is_usable(text):
        return text
    return _extract_pdf_text_builtin(pdf_bytes)


def _extract_pdf_text_builtin(pdf_bytes: bytes) -> str:
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
    *,
    endpoint_url: str,
    model: str,
    file_bytes: bytes,
    mime_type: str,
    file_name: str,
    api_key: str | None = None,
) -> str:
    """Schickt den Beleg an einen OpenAI-``/responses``-kompatiblen OCR-Endpoint.

    Erwartet als Antwort entweder ``output_text`` oder eine ``output``-Struktur mit
    ``input_text``/``output_text``-Blöcken; alle enthaltenen Textstücke werden
    zusammengeführt.
    """
    import base64

    encoded = base64.b64encode(file_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{encoded}"
    # PDFs gehen als Datei, Bilder als Bild an die /responses-API.
    if mime_type == "application/pdf":
        document_blocks = [
            {"type": "input_file", "filename": file_name, "file_data": data_url}
        ]
    else:
        document_blocks = [{"type": "input_image", "image_url": data_url}]
    try:
        body = _post_ocr_request(endpoint_url, model, document_blocks, file_name, api_key)
    except _OCRHTTPError as exc:
        # Lokale Server (z. B. LM Studio) kennen kein input_file: Scan-PDFs
        # dann als Seitenbilder schicken.
        page_images = _pdf_page_images(file_bytes) if mime_type == "application/pdf" else []
        if exc.code != 400 or not page_images:
            raise ReceiptOCRError(f"OCR-Endpoint antwortete mit HTTP {exc.code}.") from exc
        image_blocks = [
            {
                "type": "input_image",
                "image_url": f"data:{image_mime};base64,"
                + base64.b64encode(image_bytes).decode("ascii"),
            }
            for image_mime, image_bytes in page_images
        ]
        try:
            body = _post_ocr_request(endpoint_url, model, image_blocks, file_name, api_key)
        except _OCRHTTPError as retry_exc:
            raise ReceiptOCRError(
                f"OCR-Endpoint antwortete mit HTTP {retry_exc.code}."
            ) from retry_exc

    text = _collect_response_text(body)
    if not text.strip():
        raise ReceiptOCRError("OCR-Endpoint lieferte keinen Text.")
    return text


class _OCRHTTPError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _post_ocr_request(
    endpoint_url: str,
    model: str,
    document_blocks: list[dict],
    file_name: str,
    api_key: str | None,
) -> dict:
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
                    *document_blocks,
                    {"type": "input_text", "text": f"Beleg: {file_name}"},
                ],
            },
        ],
        "metadata": {"source": "openbuchhaltung-receipt-ocr"},
    }
    request = Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=llm_headers(api_key),
        method="POST",
    )
    try:
        with urlopen(request, timeout=llm_timeout(30)) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Upstream-Fehlertexte nur ins Log, nicht an den Client.
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        logger.warning("OCR-Endpoint antwortete mit HTTP %s: %s", exc.code, detail)
        raise _OCRHTTPError(exc.code) from exc
    except TimeoutError as exc:
        raise ReceiptOCRError("OCR-Endpoint antwortete nicht rechtzeitig.") from exc
    except URLError as exc:
        raise ReceiptOCRError("OCR-Endpoint ist nicht erreichbar.") from exc
    except json.JSONDecodeError as exc:
        raise ReceiptOCRError("OCR-Endpoint lieferte kein gültiges JSON.") from exc


_MAX_OCR_PAGES = 5


def _png_from_pixels(width: int, height: int, channels: int, pixels: bytes) -> bytes:
    """Kodiert 8-Bit-Graustufen-/RGB-Rohpixel verlustfrei als PNG (nur stdlib)."""
    import struct

    row = width * channels
    raw = b"".join(b"\x00" + pixels[y * row : (y + 1) * row] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    color_type = 0 if channels == 1 else 2
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _image_channels(color_space: object) -> int | None:
    if isinstance(color_space, list) and color_space:
        if color_space[0] == "/ICCBased":
            n = color_space[1].get_object().get("/N")
            return n if n in (1, 3) else None
        return None
    return {"/DeviceGray": 1, "/DeviceRGB": 3}.get(color_space)


def _xobject_image(xobject) -> tuple[str, bytes] | None:
    """Bilddaten eines PDF-Bild-XObjects als ``(mime, bytes)``, sonst ``None``."""
    filters = xobject.get("/Filter")
    filters = list(filters) if isinstance(filters, list) else [filters]
    if filters and filters[-1] == "/DCTDecode":
        # JPEG: Rohdaten bis vor die DCT-Stufe sind die JPEG-Datei.
        if len(filters) == 1:
            return "image/jpeg", xobject._data
        return None
    if xobject.get("/BitsPerComponent") != 8 or not set(filters) <= {"/FlateDecode", None}:
        return None
    channels = _image_channels(xobject.get("/ColorSpace"))
    width, height = int(xobject.get("/Width") or 0), int(xobject.get("/Height") or 0)
    pixels = xobject.get_data()
    if not channels or not width or not height or len(pixels) < width * height * channels:
        return None
    return "image/png", _png_from_pixels(width, height, channels, pixels)


def _pdf_page_images(pdf_bytes: bytes) -> list[tuple[str, bytes]]:
    """Größtes Bild je Seite (Scan) für OCR-Endpoints ohne PDF-Unterstützung."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    from io import BytesIO

    images: list[tuple[str, bytes]] = []
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        for page in reader.pages[:_MAX_OCR_PAGES]:
            xobjects = (page.get("/Resources") or {}).get("/XObject")
            candidates = []
            for ref in (xobjects.get_object().values() if xobjects else ()):
                xobject = ref.get_object()
                if xobject.get("/Subtype") != "/Image":
                    continue
                area = int(xobject.get("/Width") or 0) * int(xobject.get("/Height") or 0)
                candidates.append((area, xobject))
            for _area, xobject in sorted(candidates, key=lambda item: -item[0]):
                image = _xobject_image(xobject)
                if image is not None:
                    images.append(image)
                    break
    except Exception:  # pypdf wirft je nach Defekt sehr unterschiedliche Fehler.
        logger.warning("Seitenbilder des PDFs konnten nicht gelesen werden.", exc_info=True)
        return []
    return images


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
    ocr_api_key: str | None = None,
) -> tuple[str, str]:
    """Gewinnt Text aus einem Beleg. Gibt ``(text, quelle)`` zurück.

    ``quelle`` ist ``"text"``, ``"pdf"`` oder ``"ocr-endpoint"``. Bild-/Scan-Belege
    ohne konfigurierten Endpoint lösen einen :class:`ReceiptOCRError` aus.
    """
    kind = _classify(mime_type, file_name)

    if kind == "text":
        return sanitize_text(_decode_text(file_bytes)), "text"

    if kind == "pdf":
        pdf_text = _extract_pdf_text(file_bytes)
        # Zeichensalat (z. B. aus Fonts mit eigenem Encoding) zählt wie eine
        # fehlende Textebene – lieber OCR/Fehler als Müll analysieren.
        if _pdf_text_is_usable(pdf_text):
            return sanitize_text(pdf_text), "pdf"
        if ocr_endpoint:
            return (
                sanitize_text(
                    _ocr_via_endpoint(
                        endpoint_url=ocr_endpoint,
                        model=ocr_model,
                        file_bytes=file_bytes,
                        mime_type=mime_type or "application/pdf",
                        file_name=file_name,
                        api_key=ocr_api_key,
                    )
                ),
                "ocr-endpoint",
            )
        raise ReceiptOCRError(
            "Das PDF enthält keine auslesbare Textebene. Für gescannte Belege bitte "
            "einen KI-Zugang für den Benutzer hinterlegen (Verwaltung) oder einen "
            "OCR-Endpoint (RECEIPT_OCR_ENDPOINT_URL) konfigurieren."
        )

    if kind == "image":
        if ocr_endpoint:
            return (
                sanitize_text(
                    _ocr_via_endpoint(
                        endpoint_url=ocr_endpoint,
                        model=ocr_model,
                        file_bytes=file_bytes,
                        mime_type=mime_type or "image/png",
                        file_name=file_name,
                        api_key=ocr_api_key,
                    )
                ),
                "ocr-endpoint",
            )
        raise ReceiptOCRError(
            "Für Bild-Belege (JPG/PNG) wird ein OCR-Endpoint benötigt. Bitte einen "
            "KI-Zugang für den Benutzer hinterlegen (Verwaltung) oder "
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
    "gesamtpreis",
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
    """Erster Geldbetrag im Fenster.

    Übersprungen werden Prozentangaben (z. B. ``19 %``) und Ziffern, die Teil einer
    Kennung sind (USt-IdNr. ``DE123456789``, Rechnungsnummer ``UA12345678``) oder als
    lange Ziffernfolge ohne Trennzeichen eher eine Nummer als ein Betrag sind.
    """
    for match in re.finditer(_AMOUNT_RE, window):
        trailing = window[match.end() : match.end() + 3].lstrip()
        if trailing.startswith("%"):
            continue
        before = window[match.start() - 1] if match.start() > 0 else ""
        after = window[match.end()] if match.end() < len(window) else ""
        if before.isalnum() or after.isalpha() or after.isdigit():
            continue
        if re.fullmatch(r"-?\d{8,}", match.group(0)):
            continue
        amount = _parse_amount(match.group(0))
        if amount is not None:
            return amount
    return None


# Vor einem Schlüsselwort ("inkl. MwSt.") verweist es nicht auf den eigenen Betrag.
_QUALIFIER_RE = re.compile(r"(inkl|incl|exkl|excl|zzgl|ohne|plus)\.?\s*$", re.IGNORECASE)


def _find_amount_for_keywords(text: str, keywords: tuple[str, ...]) -> Decimal | None:
    """Sucht den Betrag, der einem der Schlüsselwörter im Text am nächsten folgt."""
    best: Decimal | None = None
    best_pos = -1
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
            if _QUALIFIER_RE.search(text[max(0, match.start() - 8) : match.start()]):
                continue
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
    """Erstes plausibles Datum (2000 bis nächstes Jahr) im Text."""
    for match in re.finditer(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})\b", text):
        day, month, year = match.groups()
        year_int = int(year)
        if year_int < 100:
            year_int += 2000
        try:
            found = date(year_int, int(month), int(day))
        except ValueError:
            continue
        if _date_plausible(found):
            return found
    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            found = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if _date_plausible(found):
            return found
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
    skip = (
        "rechnung",
        "invoice",
        "beleg",
        "quittung",
        "datum",
        "seite",
        "page",
        "receipt",
        "kassenbon",
        "ihre rechnung",
    )
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) < 3:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(token) for token in skip):
            continue
        # Adresszeilen (Straße Hausnummer bzw. PLZ Ort) und "Feld: Wert"-Zeilen
        # (Telefon, Mitarbeiter, St-Nr.) sind kein Lieferantenname.
        if re.search(r"\d+[a-z]?,?$", lowered) or re.match(r"\d{4,5}\s", stripped):
            continue
        if ":" in stripped:
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

    if tax is None and gross is not None and net is not None:
        tax = (gross - net).quantize(_CENT, rounding=ROUND_HALF_UP)

    if tax is None and net is not None and rate is not None:
        tax = (net * rate / Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)

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
    # Offensichtlich falsch zugeordnete Treffer verwerfen statt hochzurechnen.
    if gross is not None and gross <= 0:
        gross = None
    if tax is not None and (tax < 0 or (gross is not None and tax >= gross)):
        tax = None
    if net is not None and (net < 0 or (gross is not None and net > gross)):
        net = None
    if net is not None and tax is not None and tax > net * Decimal("0.28") + _CENT:
        tax = None

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
    "Du bist Buchhalter einer deutschen Firma und liest den Text eines Eingangsbelegs "
    "(Rechnung, Quittung, Kassenbon). Extrahiere die buchungsrelevanten Daten.\n"
    "Regeln:\n"
    "- supplier: Firmenname des Rechnungsstellers (Verkäufer/Leistender). Niemals der "
    "Rechnungsempfänger (\"Rechnung an\", \"Bill to\", Kunde), keine Adresse, keine "
    "Seitenangabe. Steht kein Name auf dem Beleg: null.\n"
    "- supplier_vat_id: USt-IdNr. des Rechnungsstellers (z. B. DE123456789), nie die "
    "des Empfängers.\n"
    "- invoice_number: Rechnungsnummer (\"Rechnungs-Nr.\", \"Invoice number\"), nicht "
    "Bestell-, Kunden- oder Zahlungsbelegnummer.\n"
    "- invoice_date: Rechnungs-/Ausstellungsdatum als YYYY-MM-DD, nicht Fälligkeits-, "
    "Bestell- oder Leistungsdatum.\n"
    "- currency_code: ISO-Code der Rechnungswährung (EUR, USD, ...).\n"
    "- gross_amount: Rechnungsbetrag inklusive Umsatzsteuer, ohne Trinkgeld. "
    "Anzahlungen oder bereits geleistete Zahlungen mindern ihn nicht. net_amount: "
    "Summe ohne Umsatzsteuer. tax_amount: Summe der ausgewiesenen Umsatzsteuer. Es "
    "muss gelten: net_amount + tax_amount = gross_amount.\n"
    "- tax_lines: je Steuersatz ein Eintrag {\"rate\", \"net\", \"tax\"}, z. B. 7 % "
    "(Speisen) und 19 % (Getränke) getrennt. tax_rate: der Steuersatz, bei mehreren der "
    "mit dem höchsten Nettobetrag.\n"
    "- tip_amount: ausgewiesenes Trinkgeld, sonst null.\n"
    "- reverse_charge: true, wenn der Leistungsempfänger die Steuer schuldet "
    "(Reverse Charge, Steuerschuldnerschaft des Leistungsempfängers, § 13b UStG); dann "
    "tax_amount = 0. Sonst false.\n"
    "- description: kurze deutsche Beschreibung der Leistung, höchstens 60 Zeichen "
    "(z. B. \"Hotelübernachtung 15.–16.10.\", \"ChatGPT-Abo Oktober\").\n"
    "- Beträge als positive Zahl mit Punkt als Dezimaltrennzeichen, ohne "
    "Währungssymbol. Unbekanntes = null, nichts erfinden.\n"
)

_LLM_ACCOUNT_INSTRUCTION = (
    "- expense_account_code: Nummer des Kontos aus der folgenden Liste, auf das der "
    "Beleg gebucht wird; Anlagegüter über 800 EUR netto (z. B. Computer, Möbel) auf ein "
    "Anlagekonto. null, wenn keines passt. Konten:\n"
)

_LLM_FIELDS = (
    "supplier",
    "supplier_vat_id",
    "invoice_number",
    "invoice_date",
    "currency_code",
    "gross_amount",
    "net_amount",
    "tax_amount",
    "tax_rate",
    "tax_lines",
    "tip_amount",
    "reverse_charge",
    "description",
)

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_NUMBER = {"type": ["number", "null"]}
_LLM_SCHEMA_PROPERTIES: dict[str, dict] = {
    "supplier": _NULLABLE_STRING,
    "supplier_vat_id": _NULLABLE_STRING,
    "invoice_number": _NULLABLE_STRING,
    "invoice_date": _NULLABLE_STRING,
    "currency_code": _NULLABLE_STRING,
    "gross_amount": _NULLABLE_NUMBER,
    "net_amount": _NULLABLE_NUMBER,
    "tax_amount": _NULLABLE_NUMBER,
    "tax_rate": _NULLABLE_NUMBER,
    "tax_lines": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "rate": {"type": "number"},
                "net": {"type": "number"},
                "tax": {"type": "number"},
            },
            "required": ["rate", "net", "tax"],
        },
    },
    "tip_amount": _NULLABLE_NUMBER,
    "reverse_charge": {"type": "boolean"},
    "description": _NULLABLE_STRING,
    "expense_account_code": _NULLABLE_STRING,
}

# Höchstzahl der Konten im Prompt (kleine lokale Modelle verlieren sonst den Faden).
_MAX_ACCOUNT_CHOICES = 80


def _llm_fields_payload(
    text: str,
    *,
    model: str,
    account_choices: list[tuple[str, str]] | None = None,
    structured: bool = True,
) -> dict:
    """Request-Body für die strukturierte Feldextraktion (``/responses``-Format).

    ``structured`` ergänzt Temperatur 0 und ein JSON-Schema (``text.format``) –
    lokale Modelle halten sich damit deutlich zuverlässiger an das Format.
    """
    fields = list(_LLM_FIELDS)
    instruction = _LLM_INSTRUCTION
    choices = (account_choices or [])[:_MAX_ACCOUNT_CHOICES]
    if choices:
        fields.append("expense_account_code")
        instruction += _LLM_ACCOUNT_INSTRUCTION + "\n".join(
            f"  {code} {name}" for code, name in choices
        ) + "\n"
    instruction += (
        "Antworte ausschließlich mit einem JSON-Objekt ohne Erklärung mit genau diesen "
        "Feldern: " + ", ".join(fields) + "."
    )
    payload: dict = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": instruction}]},
            {"role": "user", "content": [{"type": "input_text", "text": text[:8000]}]},
        ],
        "metadata": {"source": "openbuchhaltung-receipt-fields"},
    }
    if structured:
        payload["temperature"] = 0
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "beleg",
                "schema": {
                    "type": "object",
                    "properties": {name: _LLM_SCHEMA_PROPERTIES[name] for name in fields},
                    "required": fields,
                },
            }
        }
    return payload


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


def _to_text(value: object, max_length: int = 120) -> str | None:
    if value is None or isinstance(value, (bool, dict, list)):
        return None
    cleaned = sanitize_text(str(value)).strip()
    return cleaned[:max_length] or None


def _to_tax_lines(value: object) -> list[tuple[Decimal, Decimal, Decimal]]:
    lines: list[tuple[Decimal, Decimal, Decimal]] = []
    if not isinstance(value, list):
        return lines
    for item in value:
        if not isinstance(item, dict):
            continue
        rate, net, tax = (_to_decimal(item.get(key)) for key in ("rate", "net", "tax"))
        if rate is None or net is None or tax is None:
            continue
        lines.append((rate, net, tax))
    return lines


def _parse_llm_json(text: str) -> dict:
    """Extrahiert das (letzte vollständige) JSON-Objekt aus einer LLM-Antwort.

    Reasoning-Modelle schreiben mitunter Überlegungen mit geschweiften Klammern vor
    die Antwort; deshalb wird von hinten nach dem letzten gültigen Objekt gesucht.
    """
    end = text.rfind("}")
    if text.find("{") == -1 or end == -1:
        raise ReceiptLLMError("LLM-Antwort enthält kein JSON-Objekt.")
    decoder = json.JSONDecoder()
    found: dict | None = None
    first_error: json.JSONDecodeError | None = None
    index = text.find("{")
    while index != -1:
        try:
            data, end_index = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc
            index = text.find("{", index + 1)
            continue
        if isinstance(data, dict) and data:
            found = data
        index = text.find("{", end_index)
    if found is not None:
        return found
    if first_error is not None:
        raise ReceiptLLMError(f"LLM-JSON konnte nicht gelesen werden: {first_error}")
    raise ReceiptLLMError("LLM-JSON ist kein Objekt.")


def post_llm_payload(
    endpoint_url: str, payload: dict, api_key: str | None, *, label: str
) -> dict:
    """Schickt einen ``/responses``-Request und liefert die JSON-Antwort.

    Lehnt der Server Temperatur oder JSON-Schema ab (HTTP 400, z. B. ältere lokale
    Server oder Reasoning-Modelle ohne ``temperature``), wird einmal ohne diese
    Parameter wiederholt.
    """
    attempts = [payload]
    if "temperature" in payload or "text" in payload:
        attempts.append(
            {key: value for key, value in payload.items() if key not in {"temperature", "text"}}
        )
    for index, body in enumerate(attempts):
        request = Request(
            endpoint_url,
            data=json.dumps(body).encode("utf-8"),
            headers=llm_headers(api_key),
            method="POST",
        )
        try:
            with urlopen(request, timeout=llm_timeout(30)) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            logger.warning("%s antwortete mit HTTP %s: %s", label, exc.code, detail)
            if exc.code == 400 and index + 1 < len(attempts):
                continue
            raise ReceiptLLMError(f"{label} antwortete mit HTTP {exc.code}.") from exc
        except TimeoutError as exc:
            raise ReceiptLLMError(f"{label} antwortete nicht rechtzeitig.") from exc
        except URLError as exc:
            raise ReceiptLLMError(f"{label} ist nicht erreichbar.") from exc
        except json.JSONDecodeError as exc:
            raise ReceiptLLMError(f"{label} lieferte kein gültiges JSON.") from exc
    raise ReceiptLLMError(f"{label} lieferte keine Antwort.")  # pragma: no cover


def extract_receipt_fields_llm(
    text: str,
    *,
    endpoint_url: str,
    model: str,
    api_key: str | None = None,
    account_choices: list[tuple[str, str]] | None = None,
) -> LlmReceiptFields:
    """Lässt ein LLM die Belegfelder strukturiert (als JSON) extrahieren.

    ``account_choices`` (Kontonummer, Bezeichnung) lässt das LLM zusätzlich ein
    Aufwands- bzw. Anlagekonto vorschlagen.
    """
    if not endpoint_url:
        raise ReceiptLLMError("LLM-Endpoint ist nicht konfiguriert.")
    payload = _llm_fields_payload(text, model=model, account_choices=account_choices)
    body = post_llm_payload(endpoint_url, payload, api_key, label="LLM-Endpoint")

    data = _parse_llm_json(_collect_response_text(body))
    account_code = _match_account_code(data.get("expense_account_code"), account_choices)
    return LlmReceiptFields(
        supplier=_to_text(data.get("supplier")),
        supplier_vat_id=_to_text(data.get("supplier_vat_id"), 20),
        invoice_number=_to_text(data.get("invoice_number"), 60),
        invoice_date=_to_date(data.get("invoice_date")),
        net_amount=_to_decimal(data.get("net_amount")),
        tax_amount=_to_decimal(data.get("tax_amount")),
        gross_amount=_to_decimal(data.get("gross_amount")),
        tax_rate=_to_decimal(data.get("tax_rate")),
        currency_code=_to_text(data.get("currency_code"), 3),
        tax_lines=_to_tax_lines(data.get("tax_lines")),
        tip_amount=_to_decimal(data.get("tip_amount")),
        reverse_charge=data.get("reverse_charge") is True,
        description=_to_text(data.get("description"), 80),
        expense_account_code=account_code,
    )


def _match_account_code(
    value: object, account_choices: list[tuple[str, str]] | None
) -> str | None:
    """Kontonummer aus der LLM-Antwort einer Auswahl zuordnen.

    Modelle liefern Nummern gern als Zahl (``410`` statt ``"0410"``); führende
    Nullen werden deshalb beim Vergleich ignoriert.
    """
    raw = _to_text(value, 20)
    if not raw or not account_choices:
        return None
    raw = raw.split()[0].removesuffix(".0")
    codes = [code for code, _name in account_choices]
    if raw in codes:
        return raw
    stripped = raw.lstrip("0")
    return next((code for code in codes if stripped and code.lstrip("0") == stripped), None)


def _amounts_plausible(
    net: Decimal | None, tax: Decimal | None, gross: Decimal | None
) -> bool:
    """Ob Netto/Steuer/Brutto zusammenpassen und eine übliche Steuerquote ergeben."""
    if net is None or tax is None or gross is None:
        return False
    if gross <= 0 or net < 0 or tax < 0:
        return False
    if abs(net + tax - gross) > Decimal("0.02"):
        return False
    # Höchster Umsatzsteuersatz in der EU liegt bei 27 %.
    return tax <= net * Decimal("0.28") + _CENT


def _amount_in_text(amount: Decimal, text: str) -> bool:
    """Ob ein Betrag in einer üblichen Schreibweise im Belegtext vorkommt."""
    plain = f"{amount:.2f}"
    whole, cents = plain.split(".")
    grouped = f"{int(whole):,}"
    variants = {
        f"{whole},{cents}",
        f"{grouped.replace(',', '.')},{cents}",
        plain,
        f"{grouped}.{cents}",
    }
    return any(variant in text for variant in variants)


def _date_plausible(value: date | None) -> bool:
    if value is None:
        return False
    return 2000 <= value.year <= date.today().year + 1


def _format_rate(rate: Decimal) -> str:
    return f"{rate.normalize():f}".replace(".", ",")


def apply_llm_control(extraction: ReceiptExtraction, llm: LlmReceiptFields) -> None:
    """Führt LLM-Ergebnisse mit der Regelanalyse zusammen und prüft sie gegeneinander.

    * **Textfelder:** Lieferant, USt-IdNr. und Beschreibung kommen bevorzugt vom LLM
      (die Zeilen-Heuristik trifft oft Adressen oder Seitenangaben); Rechnungsnummer
      und -datum, wenn sie plausibel sind.
    * **Beträge:** Fehlende Werte werden ergänzt. Ist der regelbasierte Satz
      unplausibel (z. B. eine USt-IdNr. als Steuerbetrag gelesen), der des LLM aber
      stimmig, gilt der LLM-Satz ("korrigiert").
    * **Kontrolle:** Gleicher Bruttobetrag bestätigt den Vorschlag; sonst wird
      gewarnt.
    """
    extraction.llm_used = True
    det_net, det_tax = extraction.net_amount, extraction.tax_amount
    det_gross, det_rate = extraction.gross_amount, extraction.tax_rate

    # Textfelder.
    if llm.supplier:
        extraction.supplier = llm.supplier[:120]
    if llm.supplier_vat_id:
        extraction.supplier_vat_id = llm.supplier_vat_id.replace(" ", "").upper()
    if llm.invoice_number:
        compact_text = re.sub(r"\s", "", extraction.raw_text)
        if not extraction.invoice_number or re.sub(r"\s", "", llm.invoice_number) in compact_text:
            extraction.invoice_number = llm.invoice_number
    if _date_plausible(llm.invoice_date):
        extraction.invoice_date = llm.invoice_date
    elif not _date_plausible(extraction.invoice_date):
        extraction.invoice_date = None
    if llm.currency_code and llm.currency_code.strip():
        extraction.currency_code = llm.currency_code.strip().upper()
    if llm.description:
        extraction.description = llm.description
    extraction.reverse_charge = llm.reverse_charge
    extraction.suggested_account_code = llm.expense_account_code
    if llm.tip_amount is not None and llm.tip_amount > 0:
        extraction.tip_amount = llm.tip_amount

    # Beträge: beide Sätze rechnerisch vervollständigen und vergleichen.
    llm_net, llm_tax, llm_gross, llm_rate, _ = _reconcile(
        llm.net_amount,
        Decimal("0.00") if llm.reverse_charge and llm.tax_amount is None else llm.tax_amount,
        llm.gross_amount,
        llm.tax_rate,
    )
    det_plausible = _amounts_plausible(det_net, det_tax, det_gross)
    llm_plausible = (
        llm.gross_amount is not None
        and (llm.net_amount is not None or llm.tax_amount is not None)
        and _amounts_plausible(llm_net, llm_tax, llm_gross)
    )
    # Das LLM trennt Netto/Steuer (auch bei mehreren Sätzen) zuverlässiger als die
    # Schlüsselwortsuche; ein abweichender Bruttobetrag gilt nur, wenn er im Text steht.
    use_llm_amounts = llm_plausible and (
        det_gross is None
        or not det_plausible
        or abs(llm_gross - det_gross) <= _CENT
        or _amount_in_text(llm_gross, extraction.raw_text)
    )
    if use_llm_amounts:
        net, tax, gross, rate = llm_net, llm_tax, llm_gross, llm_rate
        extraction.warnings = [w for w in extraction.warnings if "Bitte prüfen" not in w]
    else:
        net = det_net if det_net is not None else llm.net_amount
        tax = det_tax if det_tax is not None else llm.tax_amount
        gross = det_gross if det_gross is not None else llm.gross_amount
        rate = det_rate if det_rate is not None else llm.tax_rate
    net, tax, gross, rate, warns = _reconcile(net, tax, gross, rate)
    extraction.net_amount = net
    extraction.tax_amount = tax
    extraction.gross_amount = gross
    extraction.tax_rate = rate
    for warning in warns:
        if warning not in extraction.warnings:
            extraction.warnings.append(warning)

    # Hinweise (keine Zweifel am Ergebnis) senken die Zuverlässigkeit nicht.
    notes = _apply_tax_lines(extraction, llm.tax_lines)

    # Kontrolle.
    if det_gross is not None and llm.gross_amount is not None:
        if abs(det_gross - llm.gross_amount) <= _CENT:
            extraction.control_status = "bestätigt"
        elif use_llm_amounts:
            extraction.control_status = "korrigiert"
            extraction.warnings.append(
                f"KI-Kontrolle: regelbasierte Beträge unplausibel (Brutto {det_gross}), "
                f"KI-Werte übernommen (Brutto {gross}). Bitte prüfen."
            )
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

    if extraction.reverse_charge:
        notes.append(
            "Reverse Charge (§ 13b UStG): Umsatzsteuer und Vorsteuer selbst berechnen "
            "und buchen."
        )
    if extraction.tip_amount:
        notes.append(
            f"Trinkgeld {extraction.tip_amount} {extraction.currency_code} zusätzlich "
            "gezahlt (ohne Umsatzsteuer)."
        )
    if extraction.reverse_charge or (
        extraction.tax_amount == 0 and llm.tax_rate is not None and llm.tax_rate == 0
    ):
        # Steuerfrei laut KI: der Regel-Hinweis "kein Steuersatz erkannt" ist erledigt.
        extraction.warnings = [
            w for w in extraction.warnings if not w.startswith("Kein Steuersatz erkannt")
        ]

    extraction.confidence = _confidence_with_control(extraction)
    extraction.warnings.extend(notes)


def _apply_tax_lines(
    extraction: ReceiptExtraction, tax_lines: list[tuple[Decimal, Decimal, Decimal]]
) -> list[str]:
    """Übernimmt die Aufteilung nach Steuersätzen, wenn sie zum Ergebnis passt.

    Liefert den Hinweis auf mehrere Steuersätze (oder nichts).
    """
    if not tax_lines or extraction.gross_amount is None:
        return []
    total = sum((net + tax for _rate, net, tax in tax_lines), Decimal("0.00"))
    if abs(total - extraction.gross_amount) > Decimal("0.02"):
        return []
    extraction.tax_lines = sorted(tax_lines, key=lambda line: -line[1])
    rates = {rate for rate, _net, _tax in tax_lines}
    if len(rates) > 1:
        extraction.tax_rate = extraction.tax_lines[0][0]
        parts = ", ".join(
            f"{_format_rate(rate)} % (netto {net}, USt {tax})"
            for rate, net, tax in extraction.tax_lines
        )
        return [f"Mehrere Steuersätze: {parts}. Der Buchungsvorschlag teilt je Steuersatz auf."]
    return []


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
    ocr_api_key: str | None = None,
    llm_api_key: str | None = None,
    account_choices: list[tuple[str, str]] | None = None,
) -> ReceiptExtraction:
    """Komplette Pipeline: Text gewinnen, regelbasiert analysieren und – falls ein
    ``llm_endpoint`` konfiguriert ist – per LLM ergänzen und gegenprüfen.

    ``account_choices`` (Kontonummer, Bezeichnung) lässt das LLM zusätzlich ein
    Konto aus dem Kontenplan vorschlagen (:attr:`ReceiptExtraction.suggested_account_code`).

    LLM-Fehler blockieren die Pipeline nicht; sie werden als Warnung vermerkt, der
    regelbasierte Vorschlag bleibt erhalten.
    """
    try:
        text, source = extract_document_text(
            file_bytes=file_bytes,
            mime_type=mime_type,
            file_name=file_name,
            ocr_endpoint=ocr_endpoint,
            ocr_model=ocr_model,
            ocr_api_key=ocr_api_key,
        )
    except ReceiptOCRError:
        raise
    except Exception as exc:
        # Defekte Dateien (z. B. beschädigte PDF-Streams) dürfen keinen
        # 500er auslösen — die Aufrufer behandeln ReceiptOCRError sauber.
        raise ReceiptOCRError(
            f"Beleg konnte nicht gelesen werden ({exc.__class__.__name__}: {exc})."
        ) from exc
    extraction = analyze_receipt_text(text)
    extraction.source = source

    if llm_endpoint:
        try:
            llm_fields = extract_receipt_fields_llm(
                text,
                endpoint_url=llm_endpoint,
                model=llm_model,
                api_key=llm_api_key,
                account_choices=account_choices,
            )
            apply_llm_control(extraction, llm_fields)
        except ReceiptLLMError as exc:
            extraction.warnings.append(f"KI-Kontrolle nicht möglich: {exc}")

    return extraction
