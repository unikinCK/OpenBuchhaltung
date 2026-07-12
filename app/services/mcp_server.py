"""MCP-Server, der die OpenBuchhaltung-REST-API (/api/v1) als Tools bereitstellt.

Der Server spricht das Model-Context-Protocol über stdio (zeilenweise
JSON-RPC 2.0) und leitet jeden Tool-Aufruf an den passenden HTTP-Endpunkt der
laufenden OpenBuchhaltung-Instanz weiter. Bewusst ohne externe Abhängigkeiten
(nur stdlib) – analog zum handgeschriebenen ``mcp_client``.

Start (die App muss unter der Basis-URL erreichbar sein)::

    OPENBUCHHALTUNG_API_URL=http://localhost:5000/api/v1 \\
    OPENBUCHHALTUNG_API_TOKEN=obk_... \\
    python -m app.services.mcp_server
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "openbuchhaltung"
SERVER_VERSION = "1.0.0"
DEFAULT_API_URL = "http://localhost:5000/api/v1"


class MCPTransportError(Exception):
    """Die OpenBuchhaltung-API ist nicht erreichbar oder liefert kein JSON."""


@dataclass(slots=True)
class ApiResponse:
    status: int
    text: str
    content_type: str = ""
    json: Any = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    http_method: str
    path: str
    arg_location: str  # "query" | "json" | "none"

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _company_id_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "company_id": {
                "type": "integer",
                "description": "ID der Gesellschaft.",
            }
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


def _period_report_schema() -> dict[str, Any]:
    """Schema für Zeitraum-Reports (GuV, Summen-/Saldenliste)."""
    return {
        "type": "object",
        "properties": {
            "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            "date_from": {
                "type": "string",
                "description": "Zeitraum-Beginn (Format JJJJ-MM-TT, optional).",
            },
            "date_to": {
                "type": "string",
                "description": "Zeitraum-Ende einschließlich (Format JJJJ-MM-TT, optional).",
            },
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


def _asof_report_schema() -> dict[str, Any]:
    """Schema für Stichtags-Reports (Bilanz)."""
    return {
        "type": "object",
        "properties": {
            "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            "date_to": {
                "type": "string",
                "description": "Stichtag: Buchungen bis einschließlich (JJJJ-MM-TT, optional).",
            },
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


# Ein Tool je REST-Endpunkt unter /api/v1 (ohne den rekursiven /mcp/call-Proxy).
TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="health",
        description="Prüft, ob die OpenBuchhaltung-API erreichbar ist.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/health",
        arg_location="none",
    ),
    ToolSpec(
        name="list_companies",
        description="Listet alle Gesellschaften im Zugriffsbereich des API-Tokens.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/companies",
        arg_location="none",
    ),
    ToolSpec(
        name="create_tenant_with_company",
        description="Legt einen neuen Mandanten samt erster Gesellschaft an.",
        input_schema={
            "type": "object",
            "properties": {
                "tenant_name": {"type": "string", "description": "Name des Mandanten."},
                "company_name": {"type": "string", "description": "Name der Gesellschaft."},
                "currency_code": {
                    "type": "string",
                    "description": "ISO-Währungscode, Standard EUR.",
                    "default": "EUR",
                },
            },
            "required": ["tenant_name", "company_name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/tenants",
        arg_location="json",
    ),
    ToolSpec(
        name="list_users",
        description="Listet Benutzer im administrierbaren Zugriffsbereich.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/users",
        arg_location="none",
    ),
    ToolSpec(
        name="create_user",
        description="Legt einen Benutzer an.",
        input_schema={
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "Benutzername."},
                "password": {"type": "string", "description": "Initiales Passwort."},
                "role": {
                    "type": "string",
                    "description": "Admin, Buchhalter, Pruefer oder Support.",
                },
                "tenant_id": {
                    "type": ["integer", "null"],
                    "description": "Optionaler Mandant; null bedeutet global.",
                },
                "is_active": {"type": "boolean", "description": "Aktivstatus."},
            },
            "required": ["username", "password"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users",
        arg_location="json",
    ),
    ToolSpec(
        name="rotate_user_api_token",
        description="Erzeugt ein neues API-Token für einen Benutzer und gibt es einmalig zurück.",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/api-token",
        arg_location="json",
    ),
    ToolSpec(
        name="set_user_active",
        description="Aktiviert oder deaktiviert einen Benutzer.",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
                "is_active": {"type": "boolean", "description": "Neuer Aktivstatus."},
            },
            "required": ["user_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="create_account",
        description="Legt ein Sachkonto für eine Gesellschaft an.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "code": {"type": "string", "description": "Kontonummer (z. B. 1200)."},
                "name": {"type": "string", "description": "Kontobezeichnung."},
                "account_type": {
                    "type": "string",
                    "description": "Kontoart, z. B. asset, income, expense, equity, liability.",
                },
            },
            "required": ["company_id", "code", "name", "account_type"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/accounts",
        arg_location="json",
    ),
    ToolSpec(
        name="import_account_chart",
        description=(
            "Importiert SKR03/SKR04 oder eine eigene Kontenrahmen-CSV für eine Gesellschaft. "
            "Für eigene CSVs content_base64, file_name und optional mime_type senden."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "chart": {"type": "string", "description": "Optional: skr03 oder skr04."},
                "file_name": {"type": "string", "description": "Originaler CSV-Dateiname."},
                "mime_type": {"type": "string", "description": "MIME-Type, z. B. text/csv."},
                "content_base64": {"type": "string", "description": "CSV-Inhalt als Base64."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/account-chart/import",
        arg_location="json",
    ),
    ToolSpec(
        name="list_accounts",
        description=(
            "Listet die Sachkonten einer Gesellschaft mit interner ID, Kontonummer (code), "
            "Bezeichnung und Kontoart. Nützlich, um Kontonummern zu finden bzw. IDs "
            "aufzulösen. Standardmäßig nur aktive Konten; include_inactive=true zeigt alle."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch inaktive Konten einschließen (Standard: false).",
                    "default": False,
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/accounts",
        arg_location="query",
    ),
    ToolSpec(
        name="create_journal_entry",
        description=(
            "Erfasst eine Buchung mit mindestens zwei Zeilen. Soll- und Haben-Summe "
            "müssen ausgeglichen sein. Das Konto je Zeile wird entweder über die interne "
            "'account_id' ODER über die Kontonummer 'account_code' (z. B. '1200') angegeben "
            "– die Kontonummer wird serverseitig zur ID aufgelöst."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "entry_date": {
                    "type": "string",
                    "description": "Belegdatum im Format JJJJ-MM-TT.",
                },
                "description": {"type": "string", "description": "Buchungstext."},
                "status": {
                    "type": "string",
                    "description": "Buchungsstatus, Standard 'posted'.",
                    "default": "posted",
                },
                "lines": {
                    "type": "array",
                    "description": "Buchungszeilen.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {
                                "type": "integer",
                                "description": (
                                    "Interne Konto-ID. Alternativ 'account_code' angeben."
                                ),
                            },
                            "account_code": {
                                "type": "string",
                                "description": (
                                    "Kontonummer (z. B. '1200'). Alternative zu 'account_id'; "
                                    "genau eines von beiden angeben."
                                ),
                            },
                            "debit_amount": {
                                "type": "string",
                                "description": "Sollbetrag als Dezimalzahl, z. B. '100.00'.",
                            },
                            "credit_amount": {
                                "type": "string",
                                "description": "Habenbetrag als Dezimalzahl, z. B. '100.00'.",
                            },
                            "description": {"type": "string"},
                            "tax_code_id": {"type": "integer"},
                        },
                        "anyOf": [
                            {"required": ["account_id"]},
                            {"required": ["account_code"]},
                        ],
                        "additionalProperties": False,
                    },
                    "minItems": 2,
                },
            },
            "required": ["company_id", "entry_date", "description", "lines"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-entries",
        arg_location="json",
    ),
    ToolSpec(
        name="list_journal_entries",
        description=(
            "Listet Buchungen einer Gesellschaft samt Buchungszeilen. Optional nach "
            "date_from/date_to, source, is_finalized und limit filtern."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "date_from": {
                    "type": "string",
                    "description": "Zeitraum-Beginn (Format JJJJ-MM-TT, optional).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Zeitraum-Ende einschließlich (Format JJJJ-MM-TT, optional).",
                },
                "source": {
                    "type": "string",
                    "description": "Buchungsquelle, z. B. manual oder storno.",
                },
                "is_finalized": {
                    "type": "boolean",
                    "description": "Nur festgeschriebene bzw. offene Buchungen.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximale Anzahl Einträge, 1 bis 500.",
                    "default": 100,
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/journal-entries",
        arg_location="query",
    ),
    ToolSpec(
        name="finalize_journal_entry",
        description=(
            "Schreibt eine Buchung fest (GoBD): danach ist sie unveränderbar; Korrekturen "
            "sind nur noch über eine Stornobuchung (reverse_journal_entry) möglich."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "journal_entry_id": {
                    "type": "integer",
                    "description": "ID der festzuschreibenden Buchung.",
                },
            },
            "required": ["journal_entry_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-entries/{journal_entry_id}/finalize",
        arg_location="json",
    ),
    ToolSpec(
        name="finalize_journal_entries_until",
        description=(
            "Schreibt alle offenen Buchungen einer Gesellschaft bis einschließlich "
            "up_to_date fest. Bereits festgeschriebene Buchungen werden übersprungen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "up_to_date": {
                    "type": "string",
                    "description": "Stichtag im Format JJJJ-MM-TT.",
                },
            },
            "required": ["company_id", "up_to_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-entries/finalize-until",
        arg_location="json",
    ),
    ToolSpec(
        name="reverse_journal_entry",
        description=(
            "Storniert eine Buchung über eine Gegenbuchung (GoBD-Storno-Prinzip): Das "
            "Original bleibt unverändert, die Stornobuchung spiegelt alle Zeilen "
            "(Soll/Haben getauscht) und wird sofort festgeschrieben. Eine Buchung kann "
            "nur einmal storniert werden; Stornobuchungen selbst sind nicht stornierbar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "journal_entry_id": {
                    "type": "integer",
                    "description": "ID der zu stornierenden Buchung.",
                },
                "reversal_date": {
                    "type": "string",
                    "description": (
                        "Stornodatum im Format JJJJ-MM-TT (optional, Standard heute); "
                        "muss in einer offenen Periode liegen."
                    ),
                },
            },
            "required": ["journal_entry_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-entries/{journal_entry_id}/reverse",
        arg_location="json",
    ),
    ToolSpec(
        name="get_trial_balance",
        description=(
            "Liefert die Summen- und Saldenliste einer Gesellschaft, optional für einen "
            "Zeitraum (date_from/date_to, Format JJJJ-MM-TT)."
        ),
        input_schema=_period_report_schema(),
        http_method="GET",
        path="/trial-balance",
        arg_location="query",
    ),
    ToolSpec(
        name="get_income_statement",
        description=(
            "Liefert die Gewinn- und Verlustrechnung (GuV) einer Gesellschaft, optional für "
            "einen Zeitraum (date_from/date_to, Format JJJJ-MM-TT). Ohne Angabe: alle Buchungen. "
            "Der ausgewertete Zeitraum steht im Feld 'period' der Antwort."
        ),
        input_schema=_period_report_schema(),
        http_method="GET",
        path="/income-statement",
        arg_location="query",
    ),
    ToolSpec(
        name="get_balance_sheet",
        description=(
            "Liefert die Bilanz einer Gesellschaft zum Stichtag date_to (Format JJJJ-MM-TT, "
            "optional; ohne Angabe alle Buchungen). Der Stichtag steht im Feld 'period.as_of'."
        ),
        input_schema=_asof_report_schema(),
        http_method="GET",
        path="/balance-sheet",
        arg_location="query",
    ),
    ToolSpec(
        name="export_trial_balance_csv",
        description="Exportiert die Summen- und Saldenliste als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/trial-balance.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_journal_csv",
        description="Exportiert das Buchungsjournal als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/journal.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_datev_csv",
        description="Exportiert den DATEV-Buchungsstapel (EXTF) als CSV.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/exports/datev.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="list_documents",
        description="Listet Belege einer Gesellschaft, optional gefiltert nach Buchung.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "journal_entry_id": {
                    "type": "integer",
                    "description": "Optional: nur Belege zu dieser Buchung.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/documents",
        arg_location="query",
    ),
    ToolSpec(
        name="upload_document",
        description=(
            "Lädt einen Beleg hoch. content_base64 enthält den Dateiinhalt als Base64; "
            "erlaubt sind PDF, JPG und PNG."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "file_name": {"type": "string", "description": "Originaler Dateiname."},
                "mime_type": {
                    "type": "string",
                    "description": "MIME-Type, z. B. application/pdf.",
                },
                "content_base64": {"type": "string", "description": "Dateiinhalt als Base64."},
                "journal_entry_id": {
                    "type": "integer",
                    "description": "Optional: direkt mit dieser Buchung verknüpfen.",
                },
            },
            "required": ["company_id", "file_name", "mime_type", "content_base64"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/documents",
        arg_location="json",
    ),
    ToolSpec(
        name="link_document",
        description=(
            "Verknüpft einen Beleg mit einer Buchung. journal_entry_id=null entfernt "
            "die Verknüpfung."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "ID des Belegs."},
                "journal_entry_id": {
                    "type": ["integer", "null"],
                    "description": "ID der Buchung oder null zum Entfernen.",
                },
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/documents/{document_id}/link",
        arg_location="json",
    ),
    ToolSpec(
        name="download_document",
        description=(
            "Liefert Beleg-Metadaten plus Dateiinhalt als Base64-JSON. Für direkten "
            "Browserdownload gibt es zusätzlich GET /api/v1/documents/{id}/download."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "ID des Belegs."},
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/documents/{document_id}/content",
        arg_location="query",
    ),
    ToolSpec(
        name="create_receipt_ocr_suggestion",
        description=(
            "Lädt einen Beleg hoch, analysiert ihn per Beleg-OCR und liefert einen "
            "Buchungsvorschlag zurück. content_base64 enthält den Dateiinhalt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "file_name": {"type": "string", "description": "Originaler Dateiname."},
                "mime_type": {
                    "type": "string",
                    "description": "MIME-Type, z. B. application/pdf.",
                },
                "content_base64": {"type": "string", "description": "Dateiinhalt als Base64."},
            },
            "required": ["company_id", "file_name", "mime_type", "content_base64"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-ocr/suggestions",
        arg_location="json",
    ),
    ToolSpec(
        name="book_receipt_ocr_suggestion",
        description="Bucht einen Beleg-OCR-Vorschlag und verknüpft den Beleg mit der Buchung.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "document_id": {"type": "integer", "description": "ID des gespeicherten Belegs."},
                "expense_account_id": {"type": "integer", "description": "Aufwandskonto."},
                "creditor_account_id": {"type": "integer", "description": "Kreditorenkonto."},
                "tax_code_id": {"type": "integer", "description": "Optionaler Steuercode."},
                "entry_date": {"type": "string", "description": "Buchungsdatum JJJJ-MM-TT."},
                "description": {"type": "string", "description": "Buchungstext."},
                "net_amount": {"type": "string", "description": "Nettobetrag."},
                "tax_amount": {"type": "string", "description": "Steuerbetrag."},
            },
            "required": [
                "company_id",
                "document_id",
                "expense_account_id",
                "creditor_account_id",
                "net_amount",
                "tax_amount",
            ],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-ocr/book",
        arg_location="json",
    ),
    ToolSpec(
        name="import_einvoice",
        description=(
            "Importiert eine E-Rechnung (XRechnung/ZUGFeRD XML), bucht sie und "
            "speichert das XML als Beleg."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "expense_account_id": {"type": "integer", "description": "Aufwandskonto."},
                "creditor_account_id": {"type": "integer", "description": "Kreditorenkonto."},
                "tax_code_id": {"type": "integer", "description": "Optionaler Steuercode."},
                "file_name": {"type": "string", "description": "Originaler XML-Dateiname."},
                "mime_type": {
                    "type": "string",
                    "description": "MIME-Type, z. B. application/xml.",
                },
                "content_base64": {"type": "string", "description": "XML-Inhalt als Base64."},
            },
            "required": [
                "company_id",
                "expense_account_id",
                "creditor_account_id",
                "file_name",
                "content_base64",
            ],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/einvoices/import",
        arg_location="json",
    ),
    ToolSpec(
        name="export_einvoice",
        description="Erzeugt eine E-Rechnung als XML und liefert sie als Base64-JSON zurück.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "syntax": {"type": "string", "description": "ubl oder cii."},
                "invoice_number": {"type": "string", "description": "Rechnungsnummer."},
                "issue_date": {"type": "string", "description": "Rechnungsdatum JJJJ-MM-TT."},
                "buyer_name": {"type": "string", "description": "Name des Käufers."},
                "buyer_street": {"type": "string", "description": "Straße des Käufers."},
                "buyer_postal_code": {"type": "string", "description": "PLZ des Käufers."},
                "buyer_city": {"type": "string", "description": "Ort des Käufers."},
                "buyer_country_code": {"type": "string", "description": "Land, z. B. DE."},
                "buyer_vat_id": {"type": "string", "description": "USt-IdNr. des Käufers."},
                "buyer_reference": {"type": "string", "description": "Leitweg-ID optional."},
                "lines": {
                    "type": "array",
                    "description": "Rechnungspositionen.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "string"},
                            "unit_price": {"type": "string"},
                            "tax_rate": {"type": "string"},
                        },
                        "required": ["name", "quantity", "unit_price"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["company_id", "invoice_number", "issue_date", "buyer_name", "lines"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/einvoices/export",
        arg_location="json",
    ),
    ToolSpec(
        name="list_bank_transactions",
        description=(
            "Listet Bankumsätze einer Gesellschaft, optional nach Status und mit "
            "Buchungsvorschlägen für offene Umsätze."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "status": {
                    "type": "string",
                    "description": "Optional: open, matched oder booked.",
                },
                "include_suggestions": {
                    "type": "boolean",
                    "description": "Optional: passende Buchungen für offene Umsätze liefern.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/bank-transactions",
        arg_location="query",
    ),
    ToolSpec(
        name="import_bank_transactions",
        description="Importiert Bankumsätze aus einer CSV-Datei. content_base64 enthält die CSV.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "bank_account_id": {"type": "integer", "description": "Bankkonto."},
                "file_name": {"type": "string", "description": "Originaler CSV-Dateiname."},
                "mime_type": {"type": "string", "description": "MIME-Type, z. B. text/csv."},
                "content_base64": {"type": "string", "description": "CSV-Inhalt als Base64."},
            },
            "required": ["company_id", "bank_account_id", "file_name", "content_base64"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/import",
        arg_location="json",
    ),
    ToolSpec(
        name="match_bank_transaction",
        description="Ordnet einen offenen Bankumsatz einer bestehenden Buchung zu.",
        input_schema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "integer", "description": "ID des Bankumsatzes."},
                "journal_entry_id": {"type": "integer", "description": "ID der Buchung."},
            },
            "required": ["transaction_id", "journal_entry_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/{transaction_id}/match",
        arg_location="json",
    ),
    ToolSpec(
        name="book_bank_transaction",
        description="Verbucht einen offenen Bankumsatz gegen ein Gegenkonto.",
        input_schema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "integer", "description": "ID des Bankumsatzes."},
                "contra_account_id": {"type": "integer", "description": "Gegenkonto."},
                "tax_code_id": {"type": "integer", "description": "Optionaler Steuercode."},
                "description": {"type": "string", "description": "Optionaler Buchungstext."},
            },
            "required": ["transaction_id", "contra_account_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/{transaction_id}/book",
        arg_location="json",
    ),
    ToolSpec(
        name="list_open_items",
        description=(
            "Listet offene Posten einer Gesellschaft; optional inklusive ausgeglichener Posten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_settled": {
                    "type": "boolean",
                    "description": "Auch ausgeglichene Posten einschließen.",
                    "default": False,
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/open-items",
        arg_location="query",
    ),
    ToolSpec(
        name="create_open_item",
        description="Legt einen offenen Posten (Debitor/Kreditor) an.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "account_id": {"type": "integer", "description": "OPOS-Konto-ID."},
                "journal_entry_id": {
                    "type": "integer",
                    "description": "Optional verknüpfte Buchung.",
                },
                "item_type": {
                    "type": "string",
                    "enum": ["receivable", "payable"],
                    "description": "Debitorisch oder kreditorisch.",
                },
                "reference": {"type": "string", "description": "Referenz/Rechnungsnummer."},
                "counterparty": {"type": "string", "description": "Kunde/Lieferant."},
                "entry_date": {"type": "string", "description": "Datum JJJJ-MM-TT."},
                "due_date": {"type": "string", "description": "Fälligkeitsdatum JJJJ-MM-TT."},
                "amount": {"type": "string", "description": "Betrag, z. B. '1190.00'."},
            },
            "required": ["company_id", "account_id", "item_type", "reference", "amount"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-items",
        arg_location="json",
    ),
    ToolSpec(
        name="settle_open_item",
        description=(
            "Gleicht einen offenen Posten ganz oder teilweise aus. Ohne Betrag wird der "
            "volle offene Betrag verwendet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "open_item_id": {"type": "integer", "description": "ID des offenen Postens."},
                "amount": {"type": "string", "description": "Optionaler Ausgleichsbetrag."},
                "bank_transaction_id": {
                    "type": "integer",
                    "description": "Optional verknüpfter Bankumsatz.",
                },
                "journal_entry_id": {
                    "type": "integer",
                    "description": "Optional verknüpfte Ausgleichsbuchung.",
                },
            },
            "required": ["open_item_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-items/{open_item_id}/settle",
        arg_location="json",
    ),
    ToolSpec(
        name="list_fiscal_years",
        description="Listet Geschäftsjahre und Perioden einer Gesellschaft.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/fiscal-years",
        arg_location="query",
    ),
    ToolSpec(
        name="create_fiscal_year",
        description="Legt ein Geschäftsjahr samt Perioden an.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "label": {"type": "string", "description": "Bezeichnung, z. B. 2026."},
                "start_date": {"type": "string", "description": "Startdatum JJJJ-MM-TT."},
                "end_date": {"type": "string", "description": "Enddatum JJJJ-MM-TT."},
            },
            "required": ["company_id", "label", "start_date", "end_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fiscal-years",
        arg_location="json",
    ),
    ToolSpec(
        name="lock_period",
        description="Sperrt eine Periode gegen weitere Buchungen.",
        input_schema={
            "type": "object",
            "properties": {
                "period_id": {"type": "integer", "description": "ID der Periode."},
                "reason": {"type": "string", "description": "Optionaler Sperrgrund."},
            },
            "required": ["period_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/periods/{period_id}/lock",
        arg_location="json",
    ),
    ToolSpec(
        name="unlock_period",
        description="Entsperrt eine Periode, sofern das Geschäftsjahr nicht abgeschlossen ist.",
        input_schema={
            "type": "object",
            "properties": {
                "period_id": {"type": "integer", "description": "ID der Periode."},
            },
            "required": ["period_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/periods/{period_id}/unlock",
        arg_location="json",
    ),
    ToolSpec(
        name="close_fiscal_year",
        description="Schließt ein Geschäftsjahr ab und sperrt seine Perioden.",
        input_schema={
            "type": "object",
            "properties": {
                "fiscal_year_id": {"type": "integer", "description": "ID des Geschäftsjahres."},
            },
            "required": ["fiscal_year_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fiscal-years/{fiscal_year_id}/close",
        arg_location="json",
    ),
    ToolSpec(
        name="set_fiscal_year_start",
        description="Setzt den Beginnmonat des regulären Geschäftsjahres einer Gesellschaft.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "start_month": {"type": "integer", "description": "Monat 1 bis 12."},
            },
            "required": ["company_id", "start_month"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/companies/{company_id}/fiscal-year-start",
        arg_location="json",
    ),
    ToolSpec(
        name="list_audit_log",
        description=(
            "Listet Audit-Log-Einträge im Zugriffsbereich des API-Tokens. Optional nach "
            "tenant_id, company_id, entity_type, action und limit filtern."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tenant_id": {"type": "integer", "description": "Mandanten-ID (nur global)."},
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "entity_type": {
                    "type": "string",
                    "description": "Objekttyp, z. B. journal_entry oder document.",
                },
                "action": {"type": "string", "description": "Aktion, z. B. created."},
                "limit": {
                    "type": "integer",
                    "description": "Maximale Anzahl Einträge, 1 bis 500.",
                    "default": 100,
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/audit-log",
        arg_location="query",
    ),
    ToolSpec(
        name="get_vat_return",
        description=(
            "Berechnet die Kennziffern der Umsatzsteuer-Voranmeldung (UStVA) für einen "
            "Meldezeitraum aus den Journaldaten, ohne sie zu speichern. Kennziffern: "
            "Kz 81/86 (Bemessungsgrundlagen 19 %/7 %, volle Euro), Kz 48 (steuerfrei), "
            "Kz 66 (Vorsteuer), Kz 83 (Zahllast/Überschuss)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "period": {
                    "type": "string",
                    "description": (
                        "Meldezeitraum: Monat 'JJJJ-MM', Quartal 'JJJJ-Qn', Halbjahr "
                        "'JJJJ-Hn' oder Jahr 'JJJJ'. Alternativ date_from/date_to."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": "Zeitraumbeginn JJJJ-MM-TT (alternativ zu period).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Zeitraumende JJJJ-MM-TT (alternativ zu period).",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/vat-return",
        arg_location="query",
    ),
    ToolSpec(
        name="list_vat_returns",
        description=(
            "Listet die festgehaltenen Umsatzsteuer-Voranmeldungen (Kennziffern-Snapshots) "
            "einer Gesellschaft."
        ),
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/vat-returns",
        arg_location="query",
    ),
    ToolSpec(
        name="create_vat_return",
        description=(
            "Hält die Umsatzsteuer-Voranmeldung für einen Meldezeitraum als "
            "unveränderlichen Kennziffern-Snapshot fest (einmalig je Gesellschaft und "
            "Zeitraum)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "period": {
                    "type": "string",
                    "description": (
                        "Meldezeitraum: Monat 'JJJJ-MM', Quartal 'JJJJ-Qn', Halbjahr "
                        "'JJJJ-Hn' oder Jahr 'JJJJ'."
                    ),
                },
            },
            "required": ["company_id", "period"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/vat-returns",
        arg_location="json",
    ),
    ToolSpec(
        name="list_elster_submissions",
        description="Listet ELSTER-Test-/Übermittlungsprotokolle einer Gesellschaft.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "vat_return_id": {
                    "type": "integer",
                    "description": "Optional: nur Protokolle dieser UStVA.",
                },
                "status": {
                    "type": "string",
                    "description": "Optional: created, transmitted oder failed.",
                },
                "transport": {
                    "type": "string",
                    "description": "Optional: mock oder eric.",
                },
                "environment": {
                    "type": "string",
                    "description": "Optional: test oder production.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/elster/submissions",
        arg_location="query",
    ),
    ToolSpec(
        name="get_elster_submission",
        description=(
            "Ruft eine einzelne ELSTER-Übermittlung inklusive XML-Nutzlast und "
            "Übermittlungsprotokoll ab."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "submission_id": {
                    "type": "integer",
                    "description": "ID der ELSTER-Übermittlung.",
                },
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/elster/submissions/{submission_id}",
        arg_location="none",
    ),
    ToolSpec(
        name="download_elster_payload",
        description="Lädt die gespeicherte XML-Nutzlast einer ELSTER-Übermittlung.",
        input_schema={
            "type": "object",
            "properties": {
                "submission_id": {
                    "type": "integer",
                    "description": "ID der ELSTER-Übermittlung.",
                },
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/elster/submissions/{submission_id}/payload.xml",
        arg_location="none",
    ),
    ToolSpec(
        name="get_elster_readiness",
        description="Prüft ELSTER-Umgebung, Mock-Transport, ERiC-Bibliothek und Zertifikat.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/elster/readiness",
        arg_location="none",
    ),
    ToolSpec(
        name="submit_vat_return_elster",
        description=(
            "Übermittelt eine festgehaltene UStVA über die ELSTER-Transportkante. "
            "Aktuell unterstützt ist transport=mock in environment=test."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vat_return_id": {"type": "integer", "description": "ID der UStVA."},
                "environment": {
                    "type": "string",
                    "description": "test oder production; aktuell mock nur mit test.",
                    "default": "test",
                },
                "transport": {
                    "type": "string",
                    "description": "mock oder eric; aktuell ist mock implementiert.",
                    "default": "mock",
                },
                "certificate_alias": {
                    "type": "string",
                    "description": "Optionaler Zertifikats-/Schlüssel-Alias.",
                },
            },
            "required": ["vat_return_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/elster/ustva/submit",
        arg_location="json",
    ),
    ToolSpec(
        name="create_fixed_asset",
        description=(
            "Legt ein Anlagegut in der Anlagenbuchhaltung an und wählt das AfA-Verfahren. "
            "method: 'linear' (lineare AfA, zeitanteilig im Zugangsjahr), 'degressive' "
            "(geometrisch-degressiv mit Übergang zur linearen AfA, benötigt degressive_rate), "
            "'leistung' (Leistungs-AfA, benötigt total_units), 'gwg' (Sofortabschreibung "
            "≤ 800 €), 'sammelposten' (Poolabschreibung über 5 Jahre) oder 'manuell'. "
            "Anlage- und Abschreibungskonto per interner ID oder Kontonummer (z. B. '0400'/'4830')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "asset_number": {"type": "string", "description": "Inventarnummer (eindeutig)."},
                "name": {"type": "string", "description": "Bezeichnung des Anlageguts."},
                "acquisition_date": {
                    "type": "string",
                    "description": "Anschaffungsdatum (JJJJ-MM-TT).",
                },
                "in_service_date": {
                    "type": "string",
                    "description": "Inbetriebnahme = AfA-Beginn (JJJJ-MM-TT, Std.: Anschaffung).",
                },
                "acquisition_cost": {
                    "type": "string",
                    "description": "Anschaffungs-/Herstellungskosten, z. B. '12000.00'.",
                },
                "method": {
                    "type": "string",
                    "enum": ["linear", "degressive", "leistung", "gwg", "sammelposten", "manuell"],
                    "description": "AfA-Verfahren.",
                },
                "useful_life_months": {
                    "type": "integer",
                    "description": "Nutzungsdauer in Monaten (für linear/degressiv).",
                },
                "degressive_rate": {
                    "type": "string",
                    "description": "Degressiver Prozentsatz p. a., z. B. '20' (nur degressive).",
                },
                "total_units": {
                    "type": "string",
                    "description": "Gesamtleistung (nur leistung), z. B. '100000'.",
                },
                "residual_value": {
                    "type": "string",
                    "description": "Restwert am Ende der Nutzung, Standard '0.00'.",
                },
                "keep_memo_value": {
                    "type": "boolean",
                    "description": "Erinnerungswert 1,00 € stehen lassen (Standard false).",
                },
                "asset_account_code": {
                    "type": "string",
                    "description": "Kontonummer des Anlagekontos, z. B. '0400'.",
                },
                "depreciation_account_code": {
                    "type": "string",
                    "description": "Kontonummer des Abschreibungskontos, z. B. '4830'.",
                },
            },
            "required": [
                "company_id",
                "asset_number",
                "name",
                "acquisition_date",
                "acquisition_cost",
                "method",
            ],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fixed-assets",
        arg_location="json",
    ),
    ToolSpec(
        name="list_fixed_assets",
        description=(
            "Listet die Anlagegüter einer Gesellschaft inkl. aktuellem Buchwert, Verfahren "
            "und Status."
        ),
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/fixed-assets",
        arg_location="query",
    ),
    ToolSpec(
        name="get_depreciation_schedule",
        description=(
            "Liefert den vollständigen Abschreibungsplan (Buchwertverlauf je Jahr) eines "
            "Anlageguts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
            },
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/fixed-assets/{asset_id}/schedule",
        arg_location="query",
    ),
    ToolSpec(
        name="post_depreciation",
        description=(
            "Verbucht die planmäßige Abschreibung (AfA) eines Anlageguts für ein "
            "Wirtschaftsjahr (Soll Abschreibungsaufwand an Anlagekonto). Bei Leistungs-AfA "
            "ist 'units' (Jahresleistung) anzugeben."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
                "fiscal_year": {"type": "integer", "description": "Wirtschaftsjahr, z. B. 2026."},
                "units": {
                    "type": "string",
                    "description": "Jahresleistung (nur Leistungs-AfA), z. B. '25000'.",
                },
            },
            "required": ["asset_id", "fiscal_year"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fixed-assets/{asset_id}/depreciation",
        arg_location="json",
    ),
    ToolSpec(
        name="record_fixed_asset_impairment",
        description="Verbucht eine außerplanmäßige Abschreibung (AfaA) für ein Anlagegut.",
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
                "fiscal_year": {"type": "integer", "description": "Wirtschaftsjahr."},
                "amount": {"type": "string", "description": "Abschreibungsbetrag."},
                "depreciation_date": {
                    "type": "string",
                    "description": "Buchungsdatum JJJJ-MM-TT.",
                },
                "note": {"type": "string", "description": "Optionaler Hinweis."},
            },
            "required": ["asset_id", "fiscal_year", "amount"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fixed-assets/{asset_id}/impairment",
        arg_location="json",
    ),
    ToolSpec(
        name="dispose_fixed_asset",
        description="Bucht den Restbuchwert aus und markiert ein Anlagegut als abgegangen.",
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
                "disposal_date": {"type": "string", "description": "Abgangsdatum JJJJ-MM-TT."},
                "proceeds": {"type": "string", "description": "Optionaler Veräußerungserlös."},
                "fiscal_year": {"type": "integer", "description": "Optionales Wirtschaftsjahr."},
            },
            "required": ["asset_id", "disposal_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fixed-assets/{asset_id}/disposal",
        arg_location="json",
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


class HttpApiClient:
    """Ruft die OpenBuchhaltung-REST-API über HTTP auf (stdlib urllib)."""

    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        url = f"{self.base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
                status = response.status
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
            status = exc.code
        except URLError as exc:
            raise MCPTransportError(
                f"OpenBuchhaltung-API nicht erreichbar unter {self.base_url}: {exc.reason}"
            ) from exc

        parsed = None
        if "application/json" in content_type:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
        return ApiResponse(status=status, text=body, content_type=content_type, json=parsed)


@dataclass(slots=True)
class MCPServer:
    """Übersetzt MCP-JSON-RPC-Nachrichten in API-Aufrufe."""

    http: HttpApiClient
    protocol_version: str = PROTOCOL_VERSION

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Verarbeitet eine JSON-RPC-Nachricht; gibt None für Notifications zurück."""
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        # Notifications (ohne id) werden nicht beantwortet.
        if message_id is None and method and method.startswith("notifications/"):
            return None

        try:
            result = self._dispatch(method, params)
        except _JsonRpcError as exc:
            return {"jsonrpc": "2.0", "id": message_id, "error": exc.to_dict()}

        if message_id is None:
            return None
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            if isinstance(requested, str) and requested:
                self.protocol_version = requested
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.to_mcp() for tool in TOOLS]}
        if method == "tools/call":
            return self._call_tool(params)
        raise _JsonRpcError(-32601, f"Unbekannte Methode: {method}")

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            raise _JsonRpcError(-32602, f"Unbekanntes Tool: {name}")
        if not isinstance(arguments, dict):
            raise _JsonRpcError(-32602, "arguments muss ein Objekt sein.")

        try:
            response = self._request_for_tool(tool, arguments)
        except MCPTransportError as exc:
            return _tool_result(str(exc), is_error=True)

        if response.json is not None:
            text = json.dumps(response.json, ensure_ascii=False, indent=2)
        else:
            text = response.text
        return _tool_result(text, is_error=not response.ok)

    def _request_for_tool(self, tool: ToolSpec, arguments: dict[str, Any]) -> ApiResponse:
        # Pfad-Platzhalter wie {asset_id} aus den Argumenten füllen; die dafür
        # verbrauchten Werte nicht zusätzlich als Query/Body senden.
        path = tool.path
        remaining = dict(arguments)
        if "{" in path:
            for key in list(remaining):
                placeholder = "{" + key + "}"
                if placeholder in path:
                    path = path.replace(placeholder, str(remaining.pop(key)))

        if tool.arg_location == "query":
            return self.http.call(tool.http_method, path, params=remaining)
        if tool.arg_location == "json":
            return self.http.call(tool.http_method, path, json_body=remaining)
        return self.http.call(tool.http_method, path)


@dataclass(slots=True)
class _JsonRpcError(Exception):
    code: int
    message: str
    data: Any = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


def _tool_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def build_server_from_env(getenv: Callable[[str], str | None] = os.environ.get) -> MCPServer:
    base_url = getenv("OPENBUCHHALTUNG_API_URL") or DEFAULT_API_URL
    token = getenv("OPENBUCHHALTUNG_API_TOKEN")
    return MCPServer(http=HttpApiClient(base_url=base_url, token=token))


def serve(server: MCPServer, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Liest zeilenweise JSON-RPC von stdin und schreibt Antworten nach stdout."""
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
            continue

        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    serve(build_server_from_env())


if __name__ == "__main__":
    main()
