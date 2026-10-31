"""Aufbereitung von Tool-Aufrufen für die Chat-Oberfläche.

Template und chat.js stellen nur dar, was hier vorbereitet wird: deutscher Titel je Tool,
lesbares Ergebnis ohne die Fremddaten-Markierung für das Modell und eine Zusammenfassung
der Argumente einer zu bestätigenden Aktion (Felder plus Tabellen, z. B. Buchungszeilen).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.services.tool_titles import tool_title
from domain.formatting import format_amount

# Felder, die in der Zusammenfassung nichts beitragen (Kontext oder Rohdaten).
_HIDDEN_KEYS = frozenset({"company_id", "content_base64", "client_sha256", "mime_type"})

_FIELD_LABELS = {
    "account_code": "Konto",
    "account_id": "Konto (ID)",
    "account_type": "Kontoart",
    "acquisition_cost": "Anschaffungskosten",
    "acquisition_date": "Anschaffungsdatum",
    "amount": "Betrag",
    "asset_id": "Anlagegut (ID)",
    "bank_account_id": "Bankkonto (ID)",
    "category_id": "Belegkategorie (ID)",
    "city": "Ort",
    "code": "Nummer",
    "contra_account_id": "Gegenkonto (ID)",
    "cost_center_id": "Kostenstelle (ID)",
    "counterparty": "Gegenpartei",
    "country_code": "Land",
    "credit_amount": "Haben",
    "creditor_account_id": "Kreditorenkonto (ID)",
    "creditor_number": "Kreditorennummer",
    "debit_amount": "Soll",
    "debtor_number": "Debitorennummer",
    "description": "Text",
    "document_date": "Belegdatum",
    "document_id": "Beleg (ID)",
    "due_date": "Fällig am",
    "email": "E-Mail",
    "entry_date": "Buchungsdatum",
    "expense_account_id": "Aufwandskonto (ID)",
    "file_name": "Datei",
    "fiscal_year": "Geschäftsjahr",
    "invoice_number": "Rechnungsnummer",
    "is_active": "Aktiv",
    "is_customer": "Kunde",
    "is_supplier": "Lieferant",
    "journal_entry_id": "Buchung (ID)",
    "line_number": "Zeile",
    "lines": "Buchungszeilen",
    "method": "Verfahren",
    "name": "Bezeichnung",
    "net_amount": "Netto",
    "notes": "Notiz",
    "open_item_id": "Offener Posten (ID)",
    "partner_id": "Geschäftspartner (ID)",
    "partner_number": "Partnernummer",
    "pattern": "Muster",
    "postal_code": "PLZ",
    "profit_center_id": "Profitcenter (ID)",
    "reason": "Grund",
    "reference": "Referenz",
    "service_date": "Leistungsdatum",
    "status": "Status",
    "street": "Straße",
    "tax_amount": "Steuer",
    "tax_code_id": "Steuercode (ID)",
    "tax_rate": "Steuersatz",
    "transaction_id": "Bankumsatz (ID)",
    "transaction_ids": "Bankumsätze (IDs)",
    "useful_life_months": "Nutzungsdauer (Monate)",
    "vat_id": "USt-IdNr.",
    "year": "Jahr",
}

_AMOUNT_KEYS = frozenset(
    {
        "acquisition_cost",
        "amount",
        "credit_amount",
        "debit_amount",
        "gross_monthly_salary",
        "net_amount",
        "proceeds",
        "residual_value",
        "tax_amount",
        "unit_price",
    }
)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_UNTRUSTED_START = re.compile(r"^\[Beginn [^\]\n]*\]\n")
_UNTRUSTED_END = re.compile(r"\n\[Ende [^\]\n]*\](?=\n… \[gekürzt[^\]\n]*\]$|$)")
_TRUNCATION_TAIL = re.compile(r"\n… \[gekürzt[^\]\n]*\]$")
RESULT_DISPLAY_LIMIT = 6000


def field_label(key: str) -> str:
    return _FIELD_LABELS.get(key) or key.replace("_", " ").capitalize()


def format_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "Ja" if value else "Nein"
    if key in _AMOUNT_KEYS and isinstance(value, (int, float, str)) and value != "":
        return format_amount(value)
    if isinstance(value, str):
        match = _ISO_DATE.match(value)
        if match and (key.endswith("_date") or key in {"valid_from", "valid_to", "next_run"}):
            return f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
        return value
    if isinstance(value, list):
        return ", ".join(format_value(key, item) for item in value)
    if isinstance(value, dict):
        return ", ".join(
            f"{field_label(sub)}: {format_value(sub, item)}"
            for sub, item in value.items()
            if item not in (None, "")
        )
    return str(value)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def action_summary(arguments: dict[str, Any]) -> dict[str, Any]:
    """Felder und Tabellen einer zu bestätigenden Aktion in lesbarer Form."""
    fields: list[list[str]] = []
    tables: list[dict[str, Any]] = []
    for key, value in (arguments or {}).items():
        if key in _HIDDEN_KEYS or _is_empty(value):
            continue
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            columns: list[str] = []
            for row in value:
                for column, cell in row.items():
                    if column not in columns and column not in _HIDDEN_KEYS and not _is_empty(cell):
                        columns.append(column)
            tables.append(
                {
                    "label": field_label(key),
                    "columns": [field_label(column) for column in columns],
                    "numeric": [column in _AMOUNT_KEYS for column in columns],
                    "rows": [
                        [
                            "" if _is_empty(row.get(column)) else format_value(column, row[column])
                            for column in columns
                        ]
                        for row in value
                    ],
                }
            )
            continue
        fields.append([field_label(key), format_value(key, value)])
    if "content_base64" in (arguments or {}):
        fields.append(["Datei", "wird mit hochgeladen"])
    return {"fields": fields, "tables": tables}


def display_result(text: str | None) -> str:
    """Tool-Ergebnis ohne Fremddaten-Markierung, JSON eingerückt, gekürzt."""
    result = (text or "").strip()
    if _UNTRUSTED_START.match(result):
        result = _UNTRUSTED_END.sub("", _UNTRUSTED_START.sub("", result, count=1)).strip()
    try:
        parsed = json.loads(result)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        result = json.dumps(parsed, ensure_ascii=False, indent=2)
    elif result[:1] in "{[":
        # Kompakt gespeichertes, aber abgeschnittenes JSON lesbar einrücken.
        tail = _TRUNCATION_TAIL.search(result)
        body = result[: tail.start()] if tail else result
        result = _indent_json_fragment(body) + ("\n… (gekürzt)" if tail else "")
    if len(result) > RESULT_DISPLAY_LIMIT:
        result = result[:RESULT_DISPLAY_LIMIT] + "\n… (gekürzt)"
    return result


def _indent_json_fragment(text: str, indent: str = "  ") -> str:
    """Rückt (auch unvollständiges) kompaktes JSON zeichenweise ein."""
    out: list[str] = []
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
            out.append(char)
        elif char in "{[":
            depth += 1
            out.append(char + "\n" + indent * depth)
        elif char in "}]":
            depth = max(depth - 1, 0)
            out.append("\n" + indent * depth + char)
        elif char == ",":
            out.append(",\n" + indent * depth)
        elif char == ":":
            out.append(": ")
        elif not char.isspace():
            out.append(char)
    return "".join(out)


def tool_call_view(call: dict[str, Any]) -> dict[str, Any]:
    """Gespeicherter Tool-Aufruf plus Anzeigefelder (title, result_display)."""
    view = dict(call)
    # Externe Werkzeuge (MCP-Server) speichern ihren Anzeigenamen selbst.
    view["title"] = call.get("title") or tool_title(str(call.get("name") or ""))
    view["result_display"] = display_result(call.get("result_text"))
    return view
