"""PDF einer Ausgangsrechnung mit den Pflichtangaben nach § 14 Abs. 4 UStG.

Das E-Rechnungs-XML wird zusätzlich als Dateianhang eingebettet. Das ergibt **kein**
zertifiziertes ZUGFeRD/Factur-X-PDF/A-3: Rechtlich maßgebliche E-Rechnung bleibt das XML
(ADR-006). Die Schrift ist die PDF-Standardschrift Helvetica; Zeichen außerhalb von
Windows-1252 erscheinen im PDF in ihrer Grundform (Ł → L), das XML enthält sie unverändert.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.company_master_data import SellerData
from app.services.einvoice_export import TaxGroup
from domain.formatting import format_amount, format_number, format_rate

MARGIN_X = 20 * mm
MARGIN_TOP = 18 * mm
FOOTER_HEIGHT = 30 * mm

_BASE_FONT = "Helvetica"
_BOLD_FONT = "Helvetica-Bold"
_TRANSLITERATION = str.maketrans(
    {"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "Ħ": "H", "ħ": "h", "ı": "i", "Ø": "O", "ø": "o"}
)

_TEXT = ParagraphStyle("text", fontName=_BASE_FONT, fontSize=9.5, leading=12.5)
_SMALL = ParagraphStyle("small", parent=_TEXT, fontSize=7, leading=8.8, textColor=colors.grey)
_SENDER = ParagraphStyle("sender", parent=_TEXT, fontSize=7, leading=9)
_TITLE = ParagraphStyle(
    "title", parent=_TEXT, fontName=_BOLD_FONT, fontSize=13.5, leading=17, spaceAfter=6
)
_CELL = ParagraphStyle("cell", parent=_TEXT, fontSize=8.5, leading=10.5)
_CELL_RIGHT = ParagraphStyle("cell_right", parent=_CELL, alignment=TA_RIGHT)
_CELL_HEAD = ParagraphStyle("cell_head", parent=_CELL, fontName=_BOLD_FONT)
_CELL_HEAD_RIGHT = ParagraphStyle("cell_head_right", parent=_CELL_HEAD, alignment=TA_RIGHT)
_NOTE = ParagraphStyle("note", parent=_TEXT, fontName=_BOLD_FONT)


@dataclass(frozen=True, slots=True)
class PdfInvoiceLine:
    description: str
    quantity: Decimal
    unit_label: str
    unit_price: Decimal
    tax_rate: Decimal
    net_amount: Decimal


@dataclass(slots=True)
class PdfInvoice:
    title: str
    invoice_number: str
    issue_date: date
    seller: SellerData
    recipient_lines: list[str]
    lines: list[PdfInvoiceLine]
    tax_groups: list[TaxGroup]
    net_total: Decimal
    tax_total: Decimal
    gross_total: Decimal
    currency_code: str
    # Zeile „Leistungsdatum“ bzw. „Leistungszeitraum“: (Bezeichnung, Wert).
    service_label: str = ""
    service_text: str = ""
    customer_number: str = ""
    buyer_reference: str = ""
    recipient_vat_id: str = ""
    # Bezug der Stornorechnung auf das Original.
    reference_text: str = ""
    header_text: str = ""
    # Pflichthinweis zur Steuerbehandlung (§ 14a UStG, § 34a UStDV).
    tax_note: str = ""
    payment_lines: list[str] = field(default_factory=list)
    footer_text: str = ""


def pdf_text(value: object) -> str:
    """Text für die Standardschrift: nicht darstellbare Zeichen in die Grundform bringen."""
    text = str(value if value is not None else "")
    try:
        text.encode("cp1252")
        return text
    except UnicodeEncodeError:
        pass
    result = []
    for char in text.translate(_TRANSLITERATION):
        try:
            char.encode("cp1252")
            result.append(char)
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(
            part for part in unicodedata.normalize("NFKD", char) if not unicodedata.combining(part)
        )
        try:
            base.encode("cp1252")
            result.append(base or "?")
        except UnicodeEncodeError:
            result.append("?")
    return "".join(result)


def _paragraph(value: object, style: ParagraphStyle) -> Paragraph:
    """Absatz aus Benutzertext: Markup wird maskiert, Zeilenumbrüche bleiben erhalten."""
    return Paragraph(escape(pdf_text(value)).replace("\n", "<br/>"), style)


def _date_text(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _meta_table(invoice: PdfInvoice) -> Table:
    rows = [("Rechnungsnummer" if not invoice.reference_text else "Stornonummer",
             invoice.invoice_number)]
    rows.append(("Rechnungsdatum", _date_text(invoice.issue_date)))
    if invoice.service_label:
        rows.append((invoice.service_label, invoice.service_text))
    if invoice.customer_number:
        rows.append(("Kundennummer", invoice.customer_number))
    if invoice.buyer_reference:
        rows.append(("Ihre Referenz", invoice.buyer_reference))
    if invoice.recipient_vat_id:
        rows.append(("Ihre USt-IdNr", invoice.recipient_vat_id))
    table = Table(
        [[_paragraph(label, _CELL), _paragraph(value, _CELL_RIGHT)] for label, value in rows],
        colWidths=[30 * mm, 40 * mm],
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
            ]
        )
    )
    return table


def _address_block(invoice: PdfInvoice, width: float) -> Table:
    recipient = [
        _paragraph(invoice.seller.sender_line, _SENDER),
        Spacer(1, 2 * mm),
        *[_paragraph(line, _TEXT) for line in invoice.recipient_lines],
    ]
    table = Table([[recipient, _meta_table(invoice)]], colWidths=[width - 74 * mm, 74 * mm])
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def _lines_table(invoice: PdfInvoice, width: float) -> Table:
    currency = invoice.currency_code
    rows: list[list[Paragraph]] = [
        [
            _paragraph("Pos.", _CELL_HEAD),
            _paragraph("Beschreibung", _CELL_HEAD),
            _paragraph("Menge", _CELL_HEAD_RIGHT),
            _paragraph("Einheit", _CELL_HEAD),
            _paragraph(f"Einzelpreis ({currency})", _CELL_HEAD_RIGHT),
            _paragraph("USt", _CELL_HEAD_RIGHT),
            _paragraph(f"Netto ({currency})", _CELL_HEAD_RIGHT),
        ]
    ]
    for index, line in enumerate(invoice.lines, start=1):
        rows.append(
            [
                _paragraph(index, _CELL),
                _paragraph(line.description, _CELL),
                _paragraph(format_number(line.quantity), _CELL_RIGHT),
                _paragraph(line.unit_label, _CELL),
                _paragraph(price_text(line.unit_price), _CELL_RIGHT),
                _paragraph(f"{format_rate(line.tax_rate)} %", _CELL_RIGHT),
                _paragraph(format_amount(line.net_amount), _CELL_RIGHT),
            ]
        )
    fixed = [9 * mm, 15 * mm, 13 * mm, 24 * mm, 11 * mm, 24 * mm]
    widths = [fixed[0], width - sum(fixed), *fixed[1:]]
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.7, colors.black),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.lightgrey),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    return table


def price_text(value: Decimal) -> str:
    """Einzelpreis mit zwei Stellen, mehr nur, wenn er sie braucht."""
    exponent = value.normalize().as_tuple().exponent
    places = max(2, -exponent if isinstance(exponent, int) else 2)
    return format_amount(value, places)


def _totals_table(invoice: PdfInvoice, width: float) -> Table:
    currency = invoice.currency_code
    rows = [
        [
            _paragraph("Summe netto", _CELL),
            _paragraph(f"{format_amount(invoice.net_total)} {currency}", _CELL_RIGHT),
        ]
    ]
    for group in invoice.tax_groups:
        rows.append(
            [
                _paragraph(
                    f"USt {format_rate(group.rate)} % auf {format_amount(group.basis)}", _CELL
                ),
                _paragraph(f"{format_amount(group.tax_amount)} {currency}", _CELL_RIGHT),
            ]
        )
    rows.append(
        [
            _paragraph("Gesamtbetrag", _CELL_HEAD),
            _paragraph(f"{format_amount(invoice.gross_total)} {currency}", _CELL_HEAD_RIGHT),
        ]
    )
    table = Table(rows, colWidths=[52 * mm, 32 * mm], hAlign="RIGHT")
    table.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, -1), (-1, -1), 0.7, colors.black),
                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
            ]
        )
    )
    return table


def _footer_columns(seller: SellerData) -> list[list[str]]:
    contact = [seller.name, seller.street, seller.postal_line]
    contact += [f"Tel. {seller.phone}" if seller.phone else "", seller.email, seller.website]
    legal = [
        f"USt-IdNr: {seller.vat_id}" if seller.vat_id else "",
        f"Steuernummer: {seller.tax_number}" if seller.tax_number else "",
        seller.register_text,
        f"Geschäftsführung: {seller.managing_directors}" if seller.managing_directors else "",
    ]
    bank = [
        seller.bank_name,
        f"IBAN {seller.iban}" if seller.iban else "",
        f"BIC {seller.bic}" if seller.bic else "",
    ]
    return [[line for line in column if line] for column in (contact, legal, bank)]


def _draw_footer(seller: SellerData):
    columns = _footer_columns(seller)

    def draw(canvas, doc) -> None:
        canvas.saveState()
        width = doc.pagesize[0] - 2 * MARGIN_X
        top = FOOTER_HEIGHT - 3 * mm
        canvas.setStrokeColor(colors.lightgrey)
        canvas.setLineWidth(0.4)
        canvas.line(MARGIN_X, top + 2 * mm, MARGIN_X + width, top + 2 * mm)
        column_width = width / 3
        for index, lines in enumerate(columns):
            x = MARGIN_X + index * column_width
            y = top
            for line in lines:
                paragraph = _paragraph(line, _SMALL)
                _, height = paragraph.wrap(column_width - 3 * mm, 30 * mm)
                y -= height
                paragraph.drawOn(canvas, x, y)
        canvas.restoreState()

    return draw


class _NumberedCanvas(pdf_canvas.Canvas):
    """Schreibt „Seite x von y“, sobald die Seitenzahl feststeht."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_pages: list[dict] = []

    def showPage(self):  # noqa: N802 (reportlab-API)
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved_pages)
        for number, state in enumerate(self._saved_pages, start=1):
            self.__dict__.update(state)
            self.setFont(_BASE_FONT, 7)
            self.setFillColor(colors.grey)
            self.drawRightString(A4[0] - MARGIN_X, 7 * mm, f"Seite {number} von {total}")
            super().showPage()
        super().save()


def build_invoice_pdf(invoice: PdfInvoice) -> bytes:
    """Erzeugt das Rechnungs-PDF; Positionen laufen mit wiederholtem Kopf über mehrere Seiten."""
    buffer = BytesIO()
    width = A4[0] - 2 * MARGIN_X
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X,
        rightMargin=MARGIN_X,
        topMargin=MARGIN_TOP,
        bottomMargin=FOOTER_HEIGHT,
        title=pdf_text(f"{invoice.title} {invoice.invoice_number}"),
        author=pdf_text(invoice.seller.name),
        creator="OpenBuchhaltung",
    )
    frame = Frame(
        MARGIN_X,
        FOOTER_HEIGHT,
        width,
        A4[1] - MARGIN_TOP - FOOTER_HEIGHT,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    document.addPageTemplates(
        [PageTemplate(id="invoice", frames=[frame], onPage=_draw_footer(invoice.seller))]
    )

    story: list = [
        _address_block(invoice, width),
        Spacer(1, 10 * mm),
        _paragraph(f"{invoice.title} {invoice.invoice_number}", _TITLE),
    ]
    if invoice.reference_text:
        story += [_paragraph(invoice.reference_text, _TEXT), Spacer(1, 2 * mm)]
    if invoice.header_text:
        story += [_paragraph(invoice.header_text, _TEXT), Spacer(1, 3 * mm)]
    story += [_lines_table(invoice, width), Spacer(1, 3 * mm)]
    closing: list = [_totals_table(invoice, width), Spacer(1, 4 * mm)]
    if invoice.tax_note:
        closing += [_paragraph(invoice.tax_note, _NOTE), Spacer(1, 3 * mm)]
    for line in invoice.payment_lines:
        closing.append(_paragraph(line, _TEXT))
    story.append(KeepTogether(closing))
    if invoice.footer_text:
        story += [Spacer(1, 4 * mm), _paragraph(invoice.footer_text, _TEXT)]

    document.build(story, canvasmaker=_NumberedCanvas)
    return buffer.getvalue()


def embed_xml_attachment(pdf_bytes: bytes, *, file_name: str, xml_bytes: bytes) -> bytes:
    """Bettet die E-Rechnung als Dateianhang ein (kein ZUGFeRD/PDF/A-3, siehe Modulkopf)."""
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf_bytes)))
    writer.add_attachment(file_name, xml_bytes)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
