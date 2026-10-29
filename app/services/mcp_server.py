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
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.tool_titles import tool_title

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "openbuchhaltung"
SERVER_VERSION = "1.0.0"
DEFAULT_API_URL = "http://localhost:5000/api/v1"

# API-Token für den laufenden Request (Thread/Kontext): der HTTP-Transport setzt hier
# den Benutzer-Token des MCP-Clients; ohne Wert gilt OPENBUCHHALTUNG_API_TOKEN.
_REQUEST_API_TOKEN: ContextVar[str | None] = ContextVar(
    "openbuchhaltung_mcp_request_api_token", default=None
)


@contextmanager
def api_token_override(token: str | None) -> Iterator[None]:
    """Ruft die REST-API im aktuellen Kontext mit ``token`` statt des Server-Tokens auf."""
    reset_token = _REQUEST_API_TOKEN.set(token)
    try:
        yield
    finally:
        _REQUEST_API_TOKEN.reset(reset_token)


def current_api_token_override() -> str | None:
    return _REQUEST_API_TOKEN.get()


# Öffentliche Basis-URL des MCP-Endpunkts für den laufenden Request: Der HTTP-Transport
# setzt hier die Adresse, unter der der Client den Server erreicht. Download-Links aus
# der API (download_path) zeigen dann dorthin statt auf die interne API-Adresse.
_PUBLIC_BASE_URL: ContextVar[str | None] = ContextVar(
    "openbuchhaltung_mcp_public_base_url", default=None
)


@contextmanager
def public_base_url_override(base_url: str | None) -> Iterator[None]:
    """Baut ``download_url`` im aktuellen Kontext aus ``base_url`` + ``download_path``."""
    reset_token = _PUBLIC_BASE_URL.set(base_url)
    try:
        yield
    finally:
        _PUBLIC_BASE_URL.reset(reset_token)


# Zeitlimit für den laufenden API-Aufruf, wenn ein Tool mehr braucht als der
# Standard (Belege per source_url: der Server lädt die Datei erst selbst).
_REQUEST_TIMEOUT: ContextVar[float | None] = ContextVar(
    "openbuchhaltung_mcp_request_timeout", default=None
)


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
    timeout: float | None = None  # None = Standard des HttpApiClient

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": tool_title(self.name),
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


_DOWNLOAD_URL_HINT = (
    "download_url unverändert als Link an den Benutzer weitergeben (ohne Anmeldung "
    "abrufbar, gültig expires_in_seconds Sekunden); keine eigenen Download-URLs bauen."
)


def _download_link_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "true = statt des Dateiinhalts einen zeitlich begrenzten Download-Link liefern "
            "(file_name, download_url). Für große Dateien und immer, wenn der Benutzer die "
            "Datei haben möchte. " + _DOWNLOAD_URL_HINT
        ),
    }


def _with_download_link(schema: dict[str, Any]) -> dict[str, Any]:
    """Ergänzt ein Export-Schema um ``download_link`` (Link statt Dateiinhalt)."""
    return {
        **schema,
        "properties": {**schema["properties"], "download_link": _download_link_property()},
    }


def _include_closing_entries_property() -> dict[str, Any]:
    return {
        "type": "boolean",
        "description": (
            "Abschlussbuchungen (Periode 13, z. B. AfA) einbeziehen; Standard false = "
            "Stand vor dem Jahresabschluss. Ergebnis- und Saldovorträge zählen in "
            "GuV/UStVA/Ertragsteuern nie mit."
        ),
        "default": False,
    }


def _capture_properties() -> dict[str, Any]:
    """Optionale Erfassungsangaben beim Belegupload (mobile Belegerfassung)."""
    return {
        "capture_source": {
            "type": "string",
            "enum": ["api", "mobile_scan", "mobile_share"],
            "description": "Erfassungsweg; Standard api.",
        },
        "captured_at": {
            "type": "string",
            "description": "Aufnahmezeitpunkt auf dem Gerät, ISO 8601 mit Zeitzone.",
        },
        "capture_device": {"type": "string", "description": "Gerätebezeichnung."},
        "client_sha256": {
            "type": "string",
            "description": (
                "SHA-256 der Datei, beim Absender berechnet. Weicht er ab, wird nichts "
                "gespeichert; ist der Beleg schon vorhanden, kommt der vorhandene zurück."
            ),
        },
    }


def _receipt_split_properties(scope: str = "") -> dict[str, Any]:
    """Aufteilung je Steuersatz und Trinkgeld für Belegbuchungen (OCR, Abgleich)."""
    return {
        "tax_lines": {
            "type": "array",
            "description": (
                f"{scope}Aufteilung bei mehreren Steuersätzen (z. B. Restaurant 7 % und "
                "19 %): je Satz Netto, Steuer und Steuercode; ersetzt net_amount, "
                "tax_amount und tax_code_id. booking_proposal.tax_lines kann direkt "
                "übernommen werden."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "net_amount": {"type": "string", "description": "Nettobetrag."},
                    "tax_amount": {"type": "string", "description": "Steuerbetrag."},
                    "tax_code_id": {
                        "type": "integer",
                        "description": "Vorsteuer-Code; ohne Angabe der Code zu rate.",
                    },
                    "rate": {"type": "string", "description": "Steuersatz in %."},
                    "expense_account_id": {
                        "type": "integer",
                        "description": "Abweichendes Aufwandskonto für diesen Satz.",
                    },
                },
                "required": ["net_amount", "tax_amount"],
                "additionalProperties": False,
            },
        },
        "tip_amount": {
            "type": "string",
            "description": (
                f"{scope}Trinkgeld; eigene Aufwandszeile ohne Steuer, erhöht den "
                "Bruttobetrag an den Kreditor."
            ),
        },
    }


# Belegtools: Server lädt source_url bis zu 30 s, danach ggf. OCR/LLM.
DOCUMENT_UPLOAD_TOOL_TIMEOUT = 120.0
# KI-Chat und Belegabgleich: mehrere LLM-Runden, lokale Modelle brauchen
# deutlich länger.
LLM_TOOL_TIMEOUT = 175.0

_SOURCE_URL_HINT = (
    "Dateiinhalt: genau eines von source_url oder content_base64. source_url "
    "bevorzugen, wenn die Quelle einen Download-Link liefert (z. B. downloadUrl eines "
    "Mail-Anhangs aus mcp-imap): Der Server lädt die Datei dann selbst, sie muss nicht "
    "durch das Sprachmodell. Scheitert der Abruf (Host nicht freigegeben, Link "
    "abgelaufen, Datei zu groß, Hash falsch), meldet das Tool den Grund; dann einen "
    "neuen Link holen oder content_base64 senden. content_base64 nur als Rückfall für "
    "kleine Dateien; dann sind file_name und mime_type Pflicht."
)


def _document_content_properties() -> dict[str, Any]:
    """Dateiinhalt eines Belegs: source_url (bevorzugt) oder content_base64."""
    return {
        "source_url": {
            "type": "string",
            "description": (
                "Bevorzugt: http(s)-Link, unter dem der Server die Datei selbst lädt "
                "(Host muss in DOCUMENT_FETCH_ALLOWED_HOSTS freigegeben sein). "
                "Nicht zusammen mit content_base64."
            ),
        },
        "content_base64": {
            "type": "string",
            "description": (
                "Rückfall: Dateiinhalt als Base64, wenn kein Link verfügbar ist oder "
                "source_url scheitert. Nicht zusammen mit source_url."
            ),
        },
        "file_name": {
            "type": "string",
            "description": (
                "Originaler Dateiname. Pflicht bei content_base64; bei source_url "
                "optional (sonst aus Content-Disposition bzw. URL)."
            ),
        },
        "mime_type": {
            "type": "string",
            "description": (
                "MIME-Type, z. B. application/pdf. Pflicht bei content_base64; bei "
                "source_url optional (sonst aus Content-Type)."
            ),
        },
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
            "include_closing_entries": _include_closing_entries_property(),
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
            "include_closing_entries": _include_closing_entries_property(),
        },
        "required": ["company_id"],
        "additionalProperties": False,
    }


def _chat_action_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "integer",
                "description": "ID der Assistenten-Nachricht mit wartender Aktion.",
            }
        },
        "required": ["message_id"],
        "additionalProperties": False,
    }


# Gemeinsame Stammdatenfelder für create_partner/update_partner.
_PARTNER_FIELD_SCHEMA: dict[str, Any] = {
    "partner_kind": {
        "type": "string",
        "enum": ["organization", "person"],
        "description": "Unternehmen (Standard) oder natürliche Person.",
    },
    "name": {"type": "string", "description": "Name bzw. Firma."},
    "is_customer": {
        "type": "boolean",
        "description": "Kunde (Debitor); vergibt eine Debitorennummer.",
    },
    "is_supplier": {
        "type": "boolean",
        "description": "Lieferant (Kreditor); vergibt eine Kreditorennummer.",
    },
    "debtor_number": {
        "type": ["string", "null"],
        "description": "Optional: Debitorennummer 10000–69999 (sonst automatisch).",
    },
    "creditor_number": {
        "type": ["string", "null"],
        "description": "Optional: Kreditorennummer 70000–99999 (sonst automatisch).",
    },
    "street": {"type": ["string", "null"]},
    "postal_code": {"type": ["string", "null"]},
    "city": {"type": ["string", "null"]},
    "country_code": {
        "type": ["string", "null"],
        "description": "ISO-Ländercode, Standard DE.",
    },
    "vat_id": {"type": ["string", "null"], "description": "USt-IdNr."},
    "tax_number": {"type": ["string", "null"], "description": "Steuernummer."},
    "email": {"type": ["string", "null"]},
    "phone": {"type": ["string", "null"]},
    "contact_person": {"type": ["string", "null"]},
    "payment_term_days": {
        "type": ["integer", "null"],
        "description": "Zahlungsziel in Tagen.",
    },
    "notes": {"type": ["string", "null"]},
    "is_active": {"type": "boolean"},
}


# Ein Tool je REST-Endpunkt unter /api/v1 (ohne die rekursive /mcp/call-Bridge).
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
        name="get_current_user",
        description=(
            "Zeigt, als wer der MCP-Zugriff arbeitet: Benutzer (Rolle, Mandant) bei "
            "Benutzer-Token, sonst globaler API-Token."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/users/me",
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
        name="set_user_llm_settings",
        description=(
            "Hinterlegt den KI-Zugang (LLM-API-Key) eines Benutzers (Administrator). "
            "Standard ist OpenAI; provider=custom mit endpoint_url für einen anderen "
            "OpenAI-/responses-kompatiblen Endpunkt. Ohne api_key bleibt ein "
            "gespeicherter Key erhalten. Der Key wird nie zurückgegeben."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
                "provider": {
                    "type": "string",
                    "enum": ["openai", "custom"],
                    "description": "openai (Standard) oder custom.",
                },
                "endpoint_url": {
                    "type": "string",
                    "description": (
                        "Nur bei custom: URL des /responses-Endpunkts "
                        "(…/v1 wird zu …/v1/responses ergänzt)."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Modellname; Standard gpt-4.1-mini. 'loaded' = das auf dem "
                        "Server (LM Studio/Ollama) gerade geladene Modell, ohne Wechsel."
                    ),
                },
                "api_key": {
                    "type": "string",
                    "description": "API-Key; bei OpenAI Pflicht, falls noch keiner hinterlegt.",
                },
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/llm",
        arg_location="json",
    ),
    ToolSpec(
        name="clear_user_llm_settings",
        description=(
            "Entfernt den KI-Zugang eines Benutzers; danach gelten die Instanz-Endpoints."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/llm/delete",
        arg_location="json",
    ),
    ToolSpec(
        name="list_oauth_grants",
        description=(
            "Listet per OAuth verbundene Apps (z. B. ChatGPT-Connector): eigene, als "
            "globaler Admin alle. include_inactive zeigt auch widerrufene/abgelaufene."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch widerrufene/abgelaufene Zugriffe anzeigen.",
                }
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/oauth/grants",
        arg_location="query",
    ),
    ToolSpec(
        name="revoke_oauth_grant",
        description="Widerruft den OAuth-Zugriff einer verbundenen App sofort.",
        input_schema={
            "type": "object",
            "properties": {
                "grant_id": {"type": "integer", "description": "ID aus list_oauth_grants."}
            },
            "required": ["grant_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/oauth/grants/{grant_id}/revoke",
        arg_location="json",
    ),
    ToolSpec(
        name="get_push_status",
        description=(
            "Status der Push-Mitteilungen an Mobilgeräte (iOS-App): ob ein Push-Relay "
            "konfiguriert ist und welche Ereignisse abonniert werden können."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/push/status",
        arg_location="query",
    ),
    ToolSpec(
        name="list_push_devices",
        description=(
            "Listet für Push-Mitteilungen registrierte Mobilgeräte: eigene, als globaler "
            "Admin alle. include_inactive zeigt auch widerrufene. Registriert werden Geräte "
            "nur von der App selbst (POST /api/v1/devices)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch widerrufene Geräte anzeigen.",
                }
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/devices",
        arg_location="query",
    ),
    ToolSpec(
        name="update_push_device",
        description=(
            "Ändert die abonnierten Ereignisse (Schlüssel aus get_push_status) und/oder "
            "den Namen eines Mobilgeräts."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer", "description": "ID aus list_push_devices."},
                "events": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Abonnierte Ereignisse, z. B. ['bank_transactions'].",
                },
                "device_name": {"type": "string", "description": "Anzeigename des Geräts."},
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/devices/{device_id}",
        arg_location="json",
    ),
    ToolSpec(
        name="revoke_push_device",
        description=(
            "Widerruft ein Mobilgerät: keine Push-Mitteilungen mehr, offene werden verworfen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer", "description": "ID aus list_push_devices."}
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/devices/{device_id}/revoke",
        arg_location="json",
    ),
    ToolSpec(
        name="send_push_test",
        description=(
            "Stellt eine Testmitteilung für ein Mobilgerät in die Warteschlange; der "
            "Dispatcher übergibt sie an das Push-Relay."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "device_id": {"type": "integer", "description": "ID aus list_push_devices."}
            },
            "required": ["device_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/devices/{device_id}/test",
        arg_location="json",
    ),
    ToolSpec(
        name="unlock_user_login",
        description=(
            "Hebt die Login-Sperre eines Benutzers auf (Rate-Limit nach zu vielen "
            "fehlgeschlagenen Anmeldeversuchen)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/unlock",
        arg_location="json",
    ),
    ToolSpec(
        name="set_user_password",
        description="Setzt das Passwort eines Benutzers neu (Administrator).",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "ID des Benutzers."},
                "new_password": {
                    "type": "string",
                    "description": "Neues Passwort (mindestens 8 Zeichen).",
                },
            },
            "required": ["user_id", "new_password"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/{user_id}/password",
        arg_location="json",
    ),
    ToolSpec(
        name="change_own_password",
        description=(
            "Ändert das Passwort des aufrufenden Benutzers (Benutzer-Token erforderlich; "
            "das aktuelle Passwort muss angegeben werden)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "current_password": {"type": "string", "description": "Aktuelles Passwort."},
                "new_password": {
                    "type": "string",
                    "description": "Neues Passwort (mindestens 8 Zeichen).",
                },
            },
            "required": ["current_password", "new_password"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/users/me/password",
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
                    "enum": ["asset", "liability", "equity", "income", "revenue", "expense"],
                    "description": "Kontoart: asset, liability, equity, income/revenue, expense.",
                },
                "subledger": {
                    "type": ["string", "null"],
                    "enum": ["debtor", "creditor", None],
                    "description": (
                        "Optional: Sammelkonto für Debitoren (debtor, Kontoart asset) oder "
                        "Kreditoren (creditor, Kontoart liability)."
                    ),
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
        name="update_account",
        description=(
            "Ändert Bezeichnung, Aktivstatus oder Sammelkonto-Kennzeichen (subledger) eines "
            "Kontos. Die Kontonummer bleibt unveränderbar, die Kontoart ebenfalls – außer zur "
            "Reparatur eines ungültigen Altwerts. Änderungen werden mit Vorher-/Nachher-Werten "
            "protokolliert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "ID des Kontos."},
                "name": {"type": "string", "description": "Neue Kontobezeichnung."},
                "is_active": {"type": "boolean", "description": "Neuer Aktivstatus."},
                "subledger": {
                    "type": ["string", "null"],
                    "enum": ["debtor", "creditor", None],
                    "description": (
                        "Sammelkonto für Debitoren (debtor) bzw. Kreditoren (creditor); "
                        "null entfernt das Kennzeichen (nur ohne Partnerbuchungen)."
                    ),
                },
                "account_type": {
                    "type": "string",
                    "enum": ["asset", "liability", "equity", "income", "revenue", "expense"],
                    "description": "Nur zur Reparatur einer ungültigen Kontoart.",
                },
            },
            "required": ["account_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/accounts/{account_id}",
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
        name="check_account_chart",
        description=(
            "Prüft den Kontenplan einer Gesellschaft, ohne etwas zu ändern. Nach dem bis "
            "Oktober 2026 fehlerhaften SKR04-Import (SKR04-Steuerkonten neben SKR03-Nummern "
            "wie 1000 Kasse, 1200 Bank, 8000 Erlöse) bestimmt sie den vorherrschenden "
            "Kontenrahmen (dominant_chart, chart_evidence) und meldet je Konto Gegenkonto, "
            "Buchungsanzahl und Saldo: bei SKR04 die SKR03-nummerierten Altkonten "
            "(legacy_skr04_accounts), bei SKR03 die SKR04-nummerierten Fremdkonten "
            "(foreign_skr04_accounts, mit verweisenden Steuercodes) und als Hinweis "
            "Nicht-Standardnummern wie 6200/8000 (nonstandard_skr03_accounts). Dazu Konten "
            "mit unbekannter Kontoart."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/account-chart/check",
        arg_location="query",
    ),
    ToolSpec(
        name="list_accounts",
        description=(
            "Listet die Sachkonten einer Gesellschaft mit interner ID, Kontonummer (code), "
            "Bezeichnung und Kontoart. Nützlich, um Kontonummern zu finden bzw. IDs "
            "aufzulösen. Standardmäßig nur aktive Konten; include_inactive=true zeigt alle. "
            "Für ein bestimmtes Konto code (exakte Kontonummer) oder q (Teil von Nummer "
            "oder Bezeichnung) angeben, statt den ganzen Kontenplan abzurufen."
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
                "code": {
                    "type": "string",
                    "description": "Nur das Konto mit genau dieser Kontonummer, z. B. \"4985\".",
                },
                "q": {
                    "type": "string",
                    "description": "Suche in Kontonummer und Bezeichnung (Teiltreffer).",
                },
                "account_type": {
                    "type": "string",
                    "enum": ["asset", "liability", "equity", "income", "expense"],
                    "description": "Nur Konten dieser Kontoart.",
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
        name="get_account_history",
        description=(
            "Liefert die unveränderbare Änderungshistorie eines Kontos mit "
            "Vorher-/Nachher-Snapshots und Audit-Hashes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "ID des Kontos."},
                "limit": {
                    "type": "integer",
                    "description": "Maximale Zahl von Einträgen (1 bis 500).",
                    "default": 100,
                },
            },
            "required": ["account_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/accounts/{account_id}/history",
        arg_location="query",
    ),
    ToolSpec(
        name="get_account_ledger",
        description=(
            "Kontoblatt eines Sachkontos: alle Buchungszeilen auf dem Konto, chronologisch, "
            "mit Belegnummer, Datum, Buchungstext, Soll/Haben, laufendem Saldo, Gegenkonten "
            "(contra_accounts), Steuercode und Geschäftspartner. Dazu Anfangssaldo "
            "(opening_balance, Stand vor date_from) und Summen. Salden = Soll minus Haben "
            "(positiv = Sollsaldo). Zeitraum per fiscal_year_id und/oder date_from/date_to; "
            "Saldovorträge erscheinen nicht als Zeilen, sondern stecken im Anfangssaldo. "
            "Das Konto per account_id angeben oder per Kontonummer: account_code zusammen "
            "mit company_id (z. B. account_code=\"4985\"). Die Konto-ID liefern auch "
            "list_accounts sowie Summen-/Saldenliste, GuV und Bilanz "
            "(account_id). Bei mehr als limit Zeilen mit offset weiterblättern. Mit sort/dir "
            "lassen sich die Zeilen nach einer Spalte ordnen; balance bleibt dabei der "
            "chronologisch laufende Saldo."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "account_id": {
                    "type": "integer",
                    "description": "ID des Kontos (alternativ account_code + company_id).",
                },
                "account_code": {
                    "type": "string",
                    "description": "Kontonummer, z. B. \"4985\" (nur zusammen mit company_id).",
                },
                "company_id": {
                    "type": "integer",
                    "description": "ID der Gesellschaft (Pflicht bei account_code).",
                },
                "fiscal_year_id": {
                    "type": "integer",
                    "description": "Geschäftsjahr (setzt date_from/date_to auf dessen Grenzen).",
                },
                "date_from": {
                    "type": "string",
                    "description": "Zeitraum-Beginn (Format JJJJ-MM-TT, optional).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Zeitraum-Ende einschließlich (Format JJJJ-MM-TT, optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximale Zahl von Zeilen (1 bis 1000).",
                    "default": 200,
                },
                "offset": {
                    "type": "integer",
                    "description": "Zeilen überspringen (Blättern).",
                    "default": 0,
                },
                "sort": {
                    "type": "string",
                    "enum": ["date", "number", "text", "contra", "debit", "credit", "balance"],
                    "description": "Sortierspalte (Standard: chronologisch).",
                },
                "dir": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Sortierrichtung (Standard: asc).",
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/accounts/ledger",
        arg_location="query",
    ),
    ToolSpec(
        name="create_controlling_unit",
        description="Legt eine Kostenstelle oder ein Profitcenter an.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "unit_type": {
                    "type": "string",
                    "enum": ["cost_center", "profit_center"],
                },
                "code": {"type": "string"},
                "name": {"type": "string"},
                "parent_id": {"type": ["integer", "null"]},
                "valid_from": {"type": ["string", "null"]},
                "valid_to": {"type": ["string", "null"]},
                "is_active": {"type": "boolean", "default": True},
            },
            "required": ["company_id", "unit_type", "code", "name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/controlling-units",
        arg_location="json",
    ),
    ToolSpec(
        name="list_controlling_units",
        description="Listet Kostenstellen und Profitcenter einer Gesellschaft.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "unit_type": {
                    "type": "string",
                    "enum": ["cost_center", "profit_center"],
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/controlling-units",
        arg_location="query",
    ),
    ToolSpec(
        name="update_controlling_unit",
        description=(
            "Ändert Bezeichnung, Hierarchie, Gültigkeit oder Aktivstatus; Code und Typ "
            "bleiben unveränderbar und Änderungen werden historisiert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "unit_id": {"type": "integer"},
                "name": {"type": "string"},
                "parent_id": {"type": ["integer", "null"]},
                "valid_from": {"type": ["string", "null"]},
                "valid_to": {"type": ["string", "null"]},
                "is_active": {"type": "boolean"},
            },
            "required": ["unit_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/controlling-units/{unit_id}",
        arg_location="json",
    ),
    ToolSpec(
        name="get_controlling_unit_history",
        description="Liefert die verkettete Vorher-/Nachher-Historie einer Einheit.",
        input_schema={
            "type": "object",
            "properties": {
                "unit_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["unit_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/controlling-units/{unit_id}/history",
        arg_location="query",
    ),
    ToolSpec(
        name="get_controlling_report",
        description=(
            "Liefert Kostenstellenrechnung oder Profitcenter-GuV (Summen je Einheit und "
            "Konto). Je Einheit zusätzlich 'investments': kontierte Bestandskonten "
            "(z. B. Anlagen im Bau) mit acquisition_cost und book_value zum Ende des "
            "Zeitraums sowie depreciation (AfA der Anlagenbuchhaltung im Zeitraum). Die "
            "einzelnen Buchungen dazu liefert list_journal_entries mit "
            "cost_center_id/profit_center_id (und account_id), 'none' für nicht zugeordnet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "unit_type": {
                    "type": "string",
                    "enum": ["cost_center", "profit_center"],
                },
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
            },
            "required": ["company_id", "unit_type"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/controlling-report",
        arg_location="query",
    ),
    ToolSpec(
        name="export_controlling_csv",
        description=(
            "Exportiert Kostenstellenrechnung oder Profitcenter-GuV als CSV, inklusive "
            "Anschaffungswert, Abschreibung und Buchwert der kontierten Bestandskonten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer"},
                "unit_type": {
                    "type": "string",
                    "enum": ["cost_center", "profit_center"],
                },
                "date_from": {"type": "string"},
                "date_to": {"type": "string"},
                "download_link": _download_link_property(),
            },
            "required": ["company_id", "unit_type"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/exports/controlling.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="list_partners",
        description=(
            "Listet Geschäftspartner (Kunden/Debitoren, Lieferanten/Kreditoren) einer "
            "Gesellschaft; optional nach Rolle (debtor/creditor) und Suchtext (Name, "
            "Nummer, USt-IdNr, Ort) gefiltert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "role": {"type": "string", "enum": ["debtor", "creditor"]},
                "q": {"type": "string", "description": "Suchtext."},
                "include_inactive": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/partners",
        arg_location="query",
    ),
    ToolSpec(
        name="get_partner",
        description="Liefert die Stammdaten eines Geschäftspartners.",
        input_schema={
            "type": "object",
            "properties": {"partner_id": {"type": "integer"}},
            "required": ["partner_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/partners/{partner_id}",
        arg_location="none",
    ),
    ToolSpec(
        name="create_partner",
        description=(
            "Legt einen Geschäftspartner an (Kunde und/oder Lieferant). Debitoren- "
            "(10000–69999) und Kreditorennummern (70000–99999) werden ohne Angabe "
            "automatisch vergeben. Bankdaten nur über set_partner_bank_details."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                **_PARTNER_FIELD_SCHEMA,
            },
            "required": ["company_id", "name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/partners",
        arg_location="json",
    ),
    ToolSpec(
        name="update_partner",
        description=(
            "Ändert Stammdaten eines Geschäftspartners (ohne Bankdaten); Rollen und "
            "Nummern sind nach Verwendung in Buchungen fest. Änderungen werden mit "
            "Vorher-/Nachher-Werten protokolliert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                **_PARTNER_FIELD_SCHEMA,
            },
            "required": ["partner_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/partners/{partner_id}",
        arg_location="json",
    ),
    ToolSpec(
        name="set_partner_bank_details",
        description=(
            "Hinterlegt die Bankverbindung (IBAN, optional BIC) eines Geschäftspartners; "
            "leere Werte löschen sie. Eigene Audit-Aktion bank_details_changed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                "iban": {"type": ["string", "null"]},
                "bic": {"type": ["string", "null"]},
            },
            "required": ["partner_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/partners/{partner_id}/bank-details",
        arg_location="json",
    ),
    ToolSpec(
        name="get_partner_history",
        description=(
            "Liefert die verkettete Vorher-/Nachher-Historie eines Geschäftspartners."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "partner_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["partner_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/partners/{partner_id}/history",
        arg_location="query",
    ),
    ToolSpec(
        name="create_journal_entry",
        description=(
            "Erfasst eine Buchung mit mindestens zwei Zeilen. Soll- und Haben-Summe "
            "müssen ausgeglichen sein. Das Konto je Zeile wird entweder über die interne "
            "'account_id' ODER über die Kontonummer 'account_code' (z. B. '1200') angegeben "
            "– die Kontonummer wird serverseitig zur ID aufgelöst. 'partner_id' bzw. "
            "'partner_number' auf oberster Ebene setzt den Geschäftspartner der Buchung "
            "(z. B. den Lieferanten bei 'Aufwand an Bank') auf alle Zeilen ohne eigenen "
            "Partner außer Steuerzeilen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "entry_date": {
                    "type": "string",
                    "description": "Belegdatum im Format JJJJ-MM-TT.",
                },
                "service_date": {
                    "type": "string",
                    "description": (
                        "Optionales Leistungsdatum (JJJJ-MM-TT; bei Leistungszeiträumen das "
                        "Ende, z. B. Rechnung vom 01.07. für Juni → '2026-06-30'). Bestimmt den "
                        "Umsatzsteuer-Meldezeitraum (UStVA/ZM); ohne Angabe gilt entry_date."
                    ),
                },
                "description": {"type": "string", "description": "Buchungstext."},
                "status": {
                    "type": "string",
                    "description": "Buchungsstatus, Standard 'posted'.",
                    "default": "posted",
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "Optionaler Geschäftspartner der Buchung (siehe list_partners); "
                        "gilt für alle Zeilen ohne eigenen Partner außer Steuerzeilen."
                    ),
                },
                "partner_number": {
                    "type": "string",
                    "description": (
                        "Alternativ zu partner_id: Debitoren-/Kreditorennummer des "
                        "Geschäftspartners der Buchung."
                    ),
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
                            "cost_center_id": {
                                "type": "integer",
                                "description": "Optionale Kostenstellen-ID.",
                            },
                            "profit_center_id": {
                                "type": "integer",
                                "description": "Optionale Profitcenter-ID.",
                            },
                            "partner_id": {
                                "type": "integer",
                                "description": (
                                    "Optionaler Geschäftspartner der Zeile. Auf Debitoren-/"
                                    "Kreditoren-Sammelkonten (Nebenbuch) muss die Rolle "
                                    "passen, auf anderen Konten ist er Partnerbezug."
                                ),
                            },
                            "partner_number": {
                                "type": "string",
                                "description": (
                                    "Alternativ zu partner_id: Debitoren-/Kreditorennummer "
                                    "(z. B. '10001' oder '70001')."
                                ),
                            },
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
        name="amend_journal_entry",
        description=(
            "Ergänzt eine offene (nicht festgeschriebene) Buchung: Leistungsdatum setzen "
            "oder entfernen und Geschäftspartner zuordnen – für die ganze Buchung per "
            "'partner_id'/'partner_number' (alle Zeilen außer Steuerzeilen, null entfernt "
            "ihn) oder je Zeile über 'lines'. Über 'lines' auch Kostenstelle und "
            "Profitcenter je Zeile setzen oder entfernen (cost_center_id/profit_center_id, "
            "IDs aus list_controlling_units; aktiv und am Buchungsdatum gültig). Auf "
            "Debitoren-/Kreditoren-Sammelkonten muss die Rolle des Partners passen. "
            "Beträge, Konten und Datum bleiben unverändert; "
            "jede Änderung wird mit altem und neuem Wert protokolliert. Festgeschriebene und "
            "stornierte Buchungen nur per Storno und Neubuchung korrigieren."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "journal_entry_id": {"type": "integer", "description": "ID der Buchung."},
                "service_date": {
                    "type": ["string", "null"],
                    "description": (
                        "Leistungsdatum JJJJ-MM-TT; null entfernt es, weglassen lässt es "
                        "unverändert."
                    ),
                },
                "partner_id": {
                    "type": ["integer", "null"],
                    "description": (
                        "Geschäftspartner der Buchung für alle Zeilen außer Steuerzeilen "
                        "(Zeilen aus 'lines' ausgenommen); null entfernt ihn, weglassen "
                        "lässt die Zuordnung unverändert."
                    ),
                },
                "partner_number": {
                    "type": "string",
                    "description": (
                        "Alternativ zu partner_id: Debitoren-/Kreditorennummer des "
                        "Geschäftspartners der Buchung."
                    ),
                },
                "lines": {
                    "type": "array",
                    "description": (
                        "Änderungen je Buchungszeile (Zeilennummer aus list_journal_entries): "
                        "Partner, Kostenstelle, Profitcenter. Nicht angegebene Felder "
                        "bleiben unverändert."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "line_number": {"type": "integer"},
                            "partner_id": {
                                "type": ["integer", "null"],
                                "description": "Partner-ID; null entfernt den Partner.",
                            },
                            "partner_number": {
                                "type": "string",
                                "description": "Alternativ: Debitoren-/Kreditorennummer.",
                            },
                            "cost_center_id": {
                                "type": ["integer", "null"],
                                "description": "Kostenstelle der Zeile; null entfernt sie.",
                            },
                            "profit_center_id": {
                                "type": ["integer", "null"],
                                "description": "Profitcenter der Zeile; null entfernt es.",
                            },
                        },
                        "required": ["line_number"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["journal_entry_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/journal-entries/{journal_entry_id}",
        arg_location="json",
    ),
    ToolSpec(
        name="list_journal_entries",
        description=(
            "Listet Buchungen einer Gesellschaft samt Buchungszeilen und den "
            "Geschäftspartnern je Buchung ('partners'). Optional nach date_from/date_to, "
            "q, source, is_finalized, partner_id, account_id, cost_center_id, "
            "profit_center_id und limit filtern. Für Saldenverlauf und Gegenkonten eines "
            "Kontos get_account_ledger verwenden."
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
                "q": {
                    "type": "string",
                    "description": "Volltextsuche in Beschreibung und Belegnummer.",
                },
                "source": {
                    "type": "string",
                    "description": "Buchungsquelle, z. B. manual oder storno.",
                },
                "is_finalized": {
                    "type": "boolean",
                    "description": "Nur festgeschriebene bzw. offene Buchungen.",
                },
                "partner_id": {
                    "type": "integer",
                    "description": "Nur Buchungen mit diesem Geschäftspartner (auf einer Zeile).",
                },
                "account_id": {
                    "type": "integer",
                    "description": (
                        "Nur Buchungen mit einer Zeile auf diesem Konto (zusammen mit "
                        "cost_center_id/profit_center_id auf derselben Zeile)."
                    ),
                },
                "cost_center_id": {
                    "type": ["integer", "string"],
                    "description": (
                        "Nur Buchungen mit einer Zeile auf dieser Kostenstelle (ID aus "
                        "list_controlling_units). 'none' = Erlös-/Aufwandszeilen ohne "
                        "Kostenstelle (\"Nicht zugeordnet\" im Controlling-Bericht)."
                    ),
                },
                "profit_center_id": {
                    "type": ["integer", "string"],
                    "description": (
                        "Nur Buchungen mit einer Zeile auf diesem Profitcenter. 'none' = "
                        "Erlös-/Aufwandszeilen ohne Profitcenter."
                    ),
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
        name="list_journal_templates",
        description=(
            "Listet Buchungsvorlagen (wiederkehrende Buchungen) mit Intervall und "
            "nächster Fälligkeit."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch deaktivierte Vorlagen einschließen.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/journal-templates",
        arg_location="query",
    ),
    ToolSpec(
        name="create_journal_template",
        description=(
            "Legt eine Buchungsvorlage an. lines ist eine Liste aus "
            "{account_id, debit, credit, optional tax_code_id/cost_center_id/"
            "profit_center_id/description}; interval on_demand, monthly, "
            "quarterly oder yearly (mit next_run als erster Fälligkeit)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "name": {"type": "string", "description": "Eindeutiger Vorlagen-Name."},
                "description": {"type": "string", "description": "Buchungstext."},
                "lines": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Buchungszeilen (account_id, debit, credit, ...).",
                },
                "interval": {
                    "type": "string",
                    "description": "on_demand (Standard), monthly, quarterly oder yearly.",
                },
                "next_run": {
                    "type": "string",
                    "description": "Erste Fälligkeit (JJJJ-MM-TT, Standard heute).",
                },
            },
            "required": ["company_id", "name", "lines"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-templates",
        arg_location="json",
    ),
    ToolSpec(
        name="set_journal_template_active",
        description="Aktiviert oder deaktiviert eine Buchungsvorlage.",
        input_schema={
            "type": "object",
            "properties": {
                "template_id": {"type": "integer", "description": "ID der Vorlage."},
                "is_active": {"type": "boolean", "description": "Neuer Status."},
            },
            "required": ["template_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-templates/{template_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="book_journal_template",
        description=(
            "Bucht eine Buchungsvorlage als normale Journalbuchung (entry_date "
            "Standard: Fälligkeit bzw. heute) und schiebt die Fälligkeit um das "
            "Intervall weiter."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "template_id": {"type": "integer", "description": "ID der Vorlage."},
                "entry_date": {
                    "type": "string",
                    "description": "Optional: Buchungsdatum (JJJJ-MM-TT).",
                },
            },
            "required": ["template_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/journal-templates/{template_id}/book",
        arg_location="json",
    ),
    ToolSpec(
        name="book_opening_balance",
        description=(
            "Eröffnungsbilanz/Saldenübernahme: bucht übergebene Kontensalden "
            "({account_id oder account_code, debit, credit}) als eine "
            "Eröffnungsbuchung; eine Differenz landet automatisch auf dem "
            "Saldenvortragskonto (9000 oder Name 'Saldenvortrag')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "entry_date": {
                    "type": "string",
                    "description": "Buchungsdatum (JJJJ-MM-TT, Beginn des Wirtschaftsjahres).",
                },
                "balances": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Kontensalden ({account_code, debit, credit}).",
                },
            },
            "required": ["company_id", "entry_date", "balances"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/opening-balance",
        arg_location="json",
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
            "nur einmal storniert werden; Stornobuchungen selbst sind nicht stornierbar. "
            "Nebenbücher werden mitgezogen: ein daraus verbuchter Bankumsatz wird wieder "
            "'open', ein AfA-Satz zurückgenommen, ein Lohnlauf wieder Entwurf, ein damit "
            "ausgeglichener offener Posten wieder offen."
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
                        "muss in einer offenen Periode liegen und darf nicht vor dem "
                        "Buchungsdatum des Originals liegen."
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
        name="get_dashboard",
        description=(
            "Kompakte Übersicht einer Gesellschaft: offene Aufgaben (unverbuchte "
            "Bankumsätze, unverknüpfte Belege, überfällige OPOS, offene "
            "Abgleich-Vorschläge, fällige Vorlagen), Bankstände je Geldkonto (Konten "
            "mit Kontoauszugsimport oder Bankverbindung sowie bebuchte Kassen- und "
            "Bankkonten des Kontenrahmens, mit letzter Buchung und letztem Umsatz), "
            "Ergebnis des laufenden Geschäftsjahres bis as_of (Format JJJJ-MM-TT, "
            "optional; Standard heute) und Bilanzsumme."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "as_of": {
                    "type": "string",
                    "description": "Stichtag (JJJJ-MM-TT), optional; Standard heute.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/dashboard",
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
        description=(
            "Exportiert die Summen- und Saldenliste als CSV (optional je Zeitraum, "
            "Saldovorträge als EB-Werte nur mit date_from)."
        ),
        input_schema=_with_download_link(_period_report_schema()),
        http_method="GET",
        path="/exports/trial-balance.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_journal_csv",
        description="Exportiert das Buchungsjournal als CSV.",
        input_schema=_with_download_link(_company_id_schema()),
        http_method="GET",
        path="/exports/journal.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_datev_csv",
        description=(
            "Exportiert den DATEV-Buchungsstapel (EXTF) eines Wirtschaftsjahres als CSV: "
            "Buchungssätze mit Konto und Gegenkonto nach dem Brutto-Prinzip (Steuer über "
            "DATEV-Automatikkonto bzw. BU-Schlüssel, sonst BU 40 und eigene Steuerzeile); "
            "Kontenrahmen SKR03/SKR04 wird erkannt und steht im Kopffeld 27. DATEV liest "
            "das Jahr des Belegdatums (TTMM) aus dem WJ-Beginn (Kopffeld 13), daher genau "
            "ein Wirtschaftsjahr je Datei: fiscal_year_id (aus list_fiscal_years) und/oder "
            "date_from/date_to innerhalb eines Wirtschaftsjahres (Kopffelder 15/16). Ohne "
            "Angabe das Wirtschaftsjahr mit Buchungen; haben mehrere Wirtschaftsjahre "
            "Buchungen, ist die Angabe Pflicht (der Fehler listet sie unter fiscal_years)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "fiscal_year_id": {
                    "type": "integer",
                    "description": "ID des Wirtschaftsjahres (list_fiscal_years), optional.",
                },
                "date_from": {
                    "type": "string",
                    "description": (
                        "Zeitraum-Beginn innerhalb des Wirtschaftsjahres (JJJJ-MM-TT, "
                        "optional; Standard: WJ-Beginn)."
                    ),
                },
                "date_to": {
                    "type": "string",
                    "description": (
                        "Zeitraum-Ende einschließlich, im selben Wirtschaftsjahr "
                        "(JJJJ-MM-TT, optional; Standard: WJ-Ende)."
                    ),
                },
                "download_link": _download_link_property(),
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/exports/datev.csv",
        arg_location="query",
    ),
    ToolSpec(
        name="export_audit_package",
        description=(
            "Erzeugt ein Prueferexport-Paket v2 mit JSON-Tabellen, Feldkatalog, "
            "Belegindex, Originalbelegen, Einzelhashes und stabilem Datenbestands-Hash. "
            "Fuer MCP liefert manifest_only=true auch den SHA-256-Hash des ZIP-Pakets; "
            "das ZIP selbst nur per download_link=true (mit manifest_only=false)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "date_from": {
                    "type": "string",
                    "description": "Optionaler Zeitraum-Beginn (Format JJJJ-MM-TT).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Optionales Zeitraum-Ende (Format JJJJ-MM-TT).",
                },
                "include_documents": {
                    "type": "boolean",
                    "description": "Originalbelege in das ZIP aufnehmen.",
                    "default": True,
                },
                "manifest_only": {
                    "type": "boolean",
                    "description": "Nur das Exportmanifest als JSON zurueckgeben.",
                    "default": True,
                },
                "download_link": _download_link_property(),
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/exports/audit-package.zip",
        arg_location="query",
    ),
    ToolSpec(
        name="list_documents",
        description=(
            "Listet Belege einer Gesellschaft, optional gefiltert nach Buchung, "
            "Belegkategorie, Dateiname (q) und Belegdatum oder nur Belege ohne "
            "Buchung (unlinked). Eine Buchung kann mehrere Belege haben (z. B. "
            "Rechnung und Zahlungsbeleg)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "journal_entry_id": {
                    "type": "integer",
                    "description": "Optional: nur Belege zu dieser Buchung.",
                },
                "unlinked": {
                    "type": "boolean",
                    "description": (
                        "Optional: nur aktuelle Belege ohne verknüpfte Buchung "
                        "(wie die Dashboard-Kachel „Belege ohne Buchung“)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional: Seitengröße (max. 1000); total nennt die Gesamtzahl.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional: Startposition für die Seitenblätterung.",
                },
                "category_id": {
                    "type": "integer",
                    "description": "Optional: nur Belege dieser Belegkategorie.",
                },
                "q": {"type": "string", "description": "Volltextsuche im Dateinamen."},
                "date_from": {
                    "type": "string",
                    "description": "Belegdatum ab (JJJJ-MM-TT, optional).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Belegdatum bis einschließlich (JJJJ-MM-TT, optional).",
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
            "Lädt einen Beleg hoch; erlaubt sind PDF, JPG und PNG. "
            + _SOURCE_URL_HINT
            + " Der Inhalt muss unverändert und vollständig sein: Zu kleine oder zum "
            "Dateityp unpassende Dateien (fehlende PDF-/JPEG-/PNG-Signatur) werden "
            "abgelehnt. client_sha256 (z. B. sha256 aus mcp-imap) wird gegen die "
            "empfangene Datei geprüft. Das Belegdatum wird getrennt vom "
            "Uploadzeitpunkt gespeichert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                **_document_content_properties(),
                "document_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Belegdatum im Format JJJJ-MM-TT.",
                },
                "journal_entry_id": {
                    "type": "integer",
                    "description": (
                        "Optional: direkt mit dieser Buchung verknüpfen (auch wenn "
                        "die Buchung schon Belege hat)."
                    ),
                },
                "category_id": {
                    "type": "integer",
                    "description": (
                        "Optional: aktive Belegkategorie (siehe list_document_categories)."
                    ),
                },
                **_capture_properties(),
            },
            "required": ["company_id", "document_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/documents",
        arg_location="json",
        timeout=DOCUMENT_UPLOAD_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="link_document",
        description=(
            "Verknüpft einen Beleg mit einer Buchung. journal_entry_id=null entfernt "
            "die Verknüpfung. Eine Buchung kann mehrere Belege haben."
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
        name="set_document_category",
        description=(
            "Ordnet einem Beleg eine Belegkategorie zu (z. B. Rechnung, Gutschrift, "
            "Zahlungsbeleg). category_id=null entfernt die Kategorie."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "ID des Belegs."},
                "category_id": {
                    "type": ["integer", "null"],
                    "description": "ID einer aktiven Belegkategorie oder null.",
                },
            },
            "required": ["document_id", "category_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/documents/{document_id}/category",
        arg_location="json",
    ),
    ToolSpec(
        name="list_document_categories",
        description=(
            "Listet die Belegkategorien einer Gesellschaft (Standard: Rechnung, "
            "Gutschrift, Zahlungsbeleg, Bewirtungsbeleg, Zahlungsanweisung sowie "
            "eigene Ergänzungen)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch deaktivierte Kategorien liefern (Standard: true).",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/document-categories",
        arg_location="query",
    ),
    ToolSpec(
        name="create_document_category",
        description=(
            "Ergänzt eine eigene Belegkategorie (nur Admin und Buchhalter). "
            "Namen sind je Gesellschaft eindeutig."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "name": {"type": "string", "description": "Name der Kategorie (max. 80 Zeichen)."},
            },
            "required": ["company_id", "name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/document-categories",
        arg_location="json",
    ),
    ToolSpec(
        name="set_document_category_active",
        description=(
            "Aktiviert oder deaktiviert eine Belegkategorie. Deaktivierte Kategorien "
            "sind für neue Zuordnungen gesperrt; bestehende Belege behalten sie."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category_id": {"type": "integer", "description": "ID der Kategorie."},
                "is_active": {"type": "boolean", "description": "true = aktiv."},
            },
            "required": ["category_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/document-categories/{category_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="replace_document",
        description=(
            "Ersetzt einen aktuellen Beleg append-only durch eine neue Version. "
            "Der alte Beleg bleibt im Versionsverlauf erhalten. " + _SOURCE_URL_HINT
        ),
        input_schema={
            "type": "object",
            "properties": {
                "document_id": {"type": "integer", "description": "ID des aktuellen Belegs."},
                **_document_content_properties(),
                "client_sha256": _capture_properties()["client_sha256"],
            },
            "required": ["document_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/documents/{document_id}/replace",
        arg_location="json",
        timeout=DOCUMENT_UPLOAD_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="download_document",
        description=(
            "Liefert Beleg-Metadaten plus Dateiinhalt als Base64 (content_base64) zum "
            "Auswerten, dazu einen zeitlich begrenzten Download-Link (download_url). Ist "
            "die Datei für den Chat zu groß oder will der Benutzer sie nur herunterladen, "
            "get_document_download_link verwenden (ohne Base64). " + _DOWNLOAD_URL_HINT
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
        name="get_document_download_link",
        description=(
            "Stellt einen Beleg zum Herunterladen bereit, ohne den Dateiinhalt zu "
            "übertragen: Metadaten, Integritätsprüfung und ein zeitlich begrenzter "
            "Download-Link (download_url). Für große Dateien und immer, wenn der Benutzer "
            "den Beleg haben möchte. " + _DOWNLOAD_URL_HINT
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
        path="/documents/{document_id}/download-link",
        arg_location="query",
    ),
    ToolSpec(
        name="create_receipt_ocr_suggestion",
        description=(
            "Lädt einen Beleg hoch, analysiert ihn per Beleg-OCR und liefert einen "
            "Buchungsvorschlag zurück: extraction (Beträge, Steuersätze je tax_lines, "
            "reverse_charge, tip_amount) und booking_proposal (Konto, Steuercode, "
            "Geschäftspartner). " + _SOURCE_URL_HINT
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                **_document_content_properties(),
                "document_date": {
                    "type": "string",
                    "format": "date",
                    "description": "Fallback-Belegdatum im Format JJJJ-MM-TT.",
                },
                **_capture_properties(),
            },
            "required": ["company_id", "document_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-ocr/suggestions",
        arg_location="json",
        timeout=DOCUMENT_UPLOAD_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="book_receipt_ocr_suggestion",
        description=(
            "Bucht einen Beleg-OCR-Vorschlag und verknüpft den Beleg mit der Buchung. "
            "Mehrere Steuersätze über tax_lines (je Satz eine Aufwands- und eine "
            "Vorsteuerzeile), Trinkgeld über tip_amount."
        ),
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
                "net_amount": {
                    "type": "string",
                    "description": "Nettobetrag (ohne tax_lines Pflicht).",
                },
                "tax_amount": {
                    "type": "string",
                    "description": "Steuerbetrag (ohne tax_lines Pflicht).",
                },
                "cost_center_id": {
                    "type": "integer",
                    "description": "Optionale Kostenstelle der Aufwandszeile.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Optionales Profitcenter der Aufwandszeile.",
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "Optionaler Geschäftspartner (Lieferant), z. B. aus booking_proposal."
                    ),
                },
                **_receipt_split_properties(),
            },
            "required": [
                "company_id",
                "document_id",
                "expense_account_id",
                "creditor_account_id",
            ],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-ocr/book",
        arg_location="json",
    ),
    ToolSpec(
        name="create_receipt_match_suggestion",
        description=(
            "Gleicht einen unverknüpften Beleg (LLM-gestützt, sonst regelbasiert) mit "
            "vorhandenen Buchungen ab. Ergebnis: Match-Vorschlag auf eine Buchung oder "
            "Vorschlag für eine neue Buchung; beides muss anschließend freigegeben werden. "
            "booking_proposal nennt Konto, Steuercode und Geschäftspartner (aus der "
            "letzten Buchung desselben Lieferanten oder per KI aus dem Kontenplan); "
            "reverse_charge und tip_amount weisen auf § 13b bzw. Trinkgeld hin."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "document_id": {
                    "type": "integer",
                    "description": "ID des unverknüpften Belegs.",
                },
            },
            "required": ["company_id", "document_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-matching/suggestions",
        arg_location="json",
        timeout=LLM_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="start_receipt_match_job",
        description=(
            "Startet den Belegabgleich wie create_receipt_match_suggestion, aber im "
            "Hintergrund, und kehrt sofort mit einem Auftrag (status running) zurück. "
            "Für langsame KI-Modelle, bei denen der direkte Abgleich in einen Timeout "
            "läuft. Den Stand liefert get_receipt_match_job."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "document_id": {
                    "type": "integer",
                    "description": "ID des unverknüpften Belegs.",
                },
            },
            "required": ["company_id", "document_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-matching/jobs",
        arg_location="json",
    ),
    ToolSpec(
        name="get_receipt_match_job",
        description=(
            "Stand eines Hintergrund-Belegabgleichs: status running, done (suggestion "
            "enthält den fertigen Vorschlag wie create_receipt_match_suggestion) oder "
            "failed (error nennt den Grund)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "ID des Auftrags."},
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["job_id", "company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/receipt-matching/jobs/{job_id}",
        arg_location="query",
    ),
    ToolSpec(
        name="list_receipt_match_suggestions",
        description=(
            "Listet Belegabgleich-Vorschläge einer Gesellschaft, optional nach Status "
            "(offen, freigegeben, abgelehnt) gefiltert – je Vorschlag mit Beleg, "
            "vorgeschlagener Buchung (inkl. Bruttobetrag) und Kontierungsvorschlag "
            "samt vorgeschlagenem Kreditorenkonto (booking_proposal.creditor_account_id)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "status": {
                    "type": "string",
                    "description": "Optionaler Statusfilter: offen, freigegeben, abgelehnt.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximale Anzahl (Standard 50).",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/receipt-matching/suggestions",
        arg_location="query",
    ),
    ToolSpec(
        name="approve_receipt_match_suggestion",
        description=(
            "Gibt einen Belegabgleich-Vorschlag frei. Match-Vorschläge verknüpfen den "
            "Beleg mit der (per journal_entry_id änderbaren) Buchung; New-Booking-"
            "Vorschläge erzeugen aus den übergebenen Feldern eine neue Buchung – bei "
            "mehreren Steuersätzen mit tax_lines (aus booking_proposal.tax_lines), "
            "Trinkgeld über tip_amount."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "suggestion_id": {"type": "integer", "description": "ID des Vorschlags."},
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "journal_entry_id": {
                    "type": "integer",
                    "description": (
                        "Nur Match-Vorschläge: abweichende Buchung statt der "
                        "vorgeschlagenen verknüpfen."
                    ),
                },
                "expense_account_id": {
                    "type": "integer",
                    "description": (
                        "Nur New-Booking: Aufwands-/Anlagekonto; ohne Angabe das Konto "
                        "aus booking_proposal des Vorschlags."
                    ),
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "Nur New-Booking: Geschäftspartner (Lieferant); ohne Angabe der "
                        "aus booking_proposal."
                    ),
                },
                "creditor_account_id": {
                    "type": "integer",
                    "description": (
                        "Nur New-Booking: Kreditorenkonto, vorgeschlagen in "
                        "booking_proposal.creditor_account_id."
                    ),
                },
                "tax_code_id": {
                    "type": "integer",
                    "description": (
                        "Nur New-Booking: Steuercode; ohne Angabe der aus booking_proposal."
                    ),
                },
                "entry_date": {
                    "type": "string",
                    "description": "Nur New-Booking: Buchungsdatum JJJJ-MM-TT.",
                },
                "description": {
                    "type": "string",
                    "description": "Nur New-Booking: Buchungstext.",
                },
                "net_amount": {
                    "type": "string",
                    "description": "Nur New-Booking: Nettobetrag.",
                },
                "tax_amount": {
                    "type": "string",
                    "description": "Nur New-Booking: Steuerbetrag.",
                },
                "cost_center_id": {
                    "type": "integer",
                    "description": "Nur New-Booking: optionale Kostenstelle.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Nur New-Booking: optionales Profitcenter.",
                },
                **_receipt_split_properties("Nur New-Booking: "),
            },
            "required": ["suggestion_id", "company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-matching/suggestions/{suggestion_id}/approve",
        arg_location="json",
    ),
    ToolSpec(
        name="reject_receipt_match_suggestion",
        description=(
            "Lehnt einen offenen Belegabgleich-Vorschlag ab; der Beleg bleibt unverknüpft."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "suggestion_id": {"type": "integer", "description": "ID des Vorschlags."},
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["suggestion_id", "company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/receipt-matching/suggestions/{suggestion_id}/reject",
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
                "tax_code_id": {
                    "type": "integer",
                    "description": (
                        "Optionaler Steuercode. Bei mehreren Steuersätzen gilt er für den "
                        "Satz, der zu ihm passt."
                    ),
                },
                "tax_lines": {
                    "type": "array",
                    "description": (
                        "Optionale Kontierung je Steuersatz der Rechnung. Rechnungen mit "
                        "mehreren Sätzen (z. B. 7 % und 19 %) werden je Satz gebucht; "
                        "ohne Angabe gilt der Vorsteuer-Code mit diesem Satz. Beträge "
                        "kommen immer aus dem XML."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "rate": {"type": "string", "description": "Steuersatz, z. B. 7."},
                            "tax_code_id": {
                                "type": "integer",
                                "description": "Vorsteuer-Code für diesen Satz.",
                            },
                            "expense_account_id": {
                                "type": "integer",
                                "description": "Abweichendes Aufwandskonto für diesen Satz.",
                            },
                        },
                        "required": ["rate"],
                        "additionalProperties": False,
                    },
                },
                "cost_center_id": {
                    "type": "integer",
                    "description": "Optionale Kostenstelle der Aufwandszeile.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Optionales Profitcenter der Aufwandszeile.",
                },
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
        name="list_bank_accounts",
        description=(
            "Listet die als Bankkonto nutzbaren Sachkonten (Kontoart asset) einer "
            "Gesellschaft. Die id dient als bank_account_id beim Import von "
            "Bankumsätzen. Standardmäßig nur aktive Konten; include_inactive=true "
            "zeigt alle."
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
        path="/bank-accounts",
        arg_location="query",
    ),
    ToolSpec(
        name="create_bank_account",
        description=(
            "Legt ein Bankkonto als Sachkonto mit Kontoart asset an (z. B. Kontonummer "
            "1210 im SKR03 bzw. 1810 im SKR04 für ein weiteres Girokonto). Die "
            "zurückgegebene id wird als bank_account_id beim Import von Bankumsätzen "
            "verwendet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "code": {
                    "type": "string",
                    "description": "Kontonummer (z. B. 1210 im SKR03, 1810 im SKR04).",
                },
                "name": {
                    "type": "string",
                    "description": "Kontobezeichnung, z. B. Bankname und Kontoart.",
                },
            },
            "required": ["company_id", "code", "name"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-accounts",
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
                "bank_account_id": {
                    "type": "integer",
                    "description": "Optional: nur Umsätze dieses Bankkontos (Konto-ID).",
                },
                "include_suggestions": {
                    "type": "boolean",
                    "description": (
                        "Optional: passende Buchungen für offene Umsätze liefern; "
                        "erkennt außerdem Übertrage zwischen eigenen Bankkonten "
                        "(transfer_counterpart_id) und liefert das Geldtransit-Konto "
                        "(geldtransit_account_id) — solche Übertrage gegen Geldtransit "
                        "buchen, nie direkt gegen das andere Bankkonto. booking_proposal "
                        "je offenem Umsatz: Gegenkonto, Steuercode, Kontierung und "
                        "Partner aus Regel (source=rule), Geldtransit (transfer) oder der "
                        "letzten Buchung derselben Gegenseite (history)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional: Seitengröße (Standard 200, max. 1000).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Optional: Startposition für die Seitenblätterung.",
                },
                "q": {
                    "type": "string",
                    "description": (
                        "Volltextsuche in Verwendungszweck, Gegenseite und Bankreferenz."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": "Buchungstag ab (JJJJ-MM-TT, optional).",
                },
                "date_to": {
                    "type": "string",
                    "description": "Buchungstag bis einschließlich (JJJJ-MM-TT, optional).",
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
        name="list_bank_booking_rules",
        description=(
            "Listet Auto-Kontierungsregeln für Bankumsätze (Teilstring-Muster auf "
            "Verwendungszweck/Gegenseite -> Gegenkonto, optional Steuercode und "
            "Controlling-Dimensionen)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch deaktivierte Regeln einschließen.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/bank-booking-rules",
        arg_location="query",
    ),
    ToolSpec(
        name="create_bank_booking_rule",
        description=(
            "Legt eine Auto-Kontierungsregel an: Trifft das Muster (Teilstring, "
            "ohne Groß-/Kleinschreibung, min. 3 Zeichen) auf Verwendungszweck oder "
            "Gegenseite zu, wird das Gegenkonto vorgeschlagen bzw. beim Regel-Lauf "
            "direkt verbucht. Das längste Muster gewinnt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "pattern": {"type": "string", "description": "Teilstring-Muster."},
                "contra_account_id": {"type": "integer", "description": "Gegenkonto-ID."},
                "tax_code_id": {"type": "integer", "description": "Optionaler Steuercode."},
                "cost_center_id": {"type": "integer", "description": "Optionale Kostenstelle."},
                "profit_center_id": {
                    "type": "integer",
                    "description": "Optionales Profitcenter.",
                },
            },
            "required": ["company_id", "pattern", "contra_account_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-booking-rules",
        arg_location="json",
    ),
    ToolSpec(
        name="set_bank_booking_rule_active",
        description="Aktiviert oder deaktiviert eine Auto-Kontierungsregel.",
        input_schema={
            "type": "object",
            "properties": {
                "rule_id": {"type": "integer", "description": "ID der Regel."},
                "is_active": {"type": "boolean", "description": "Neuer Status."},
            },
            "required": ["rule_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-booking-rules/{rule_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="apply_bank_booking_rules",
        description=(
            "Regel-Lauf: verbucht alle offenen Bankumsätze mit Regel-Treffer "
            "(optional auf transaction_ids eingeschränkt). Fehler einzelner "
            "Buchungen brechen den Lauf nicht ab, sondern stehen im Report."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "transaction_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional: nur diese Umsätze verbuchen.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-booking-rules/apply",
        arg_location="json",
    ),
    ToolSpec(
        name="get_bank_reconciliation",
        description=(
            "Saldenabgleich je Bankkonto: Buchsaldo (Soll − Haben aller "
            "Buchungszeilen) gegen die Summe der importierten Kontoauszugszeilen; "
            "macht schief hängende Bankkonten sichtbar."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/bank-reconciliation",
        arg_location="query",
    ),
    ToolSpec(
        name="import_bank_transactions",
        description=(
            "Importiert Bankumsätze aus einer Kontoauszugsdatei (CSV, CAMT.053-XML "
            "oder MT940/.sta). content_base64 enthält die Datei. CSV: Spalten-Aliasse "
            "Buchungstag/Datum, Betrag, Verwendungszweck, Auftraggeber/Empfänger; der "
            "Original-Export der Finom-Bank (Spalten Transaktions-ID, Zahlungsbetrag) "
            "wird erkannt (nur Status Completed, Kartenzahlungen als 'Kartenzahlung "
            "<Empfänger>', Transaktions-ID als Bankreferenz). Re-Importe werden "
            "dedupliziert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "bank_account_id": {
                    "type": "integer",
                    "description": (
                        "ID des Bank-Sachkontos (siehe list_bank_accounts bzw. "
                        "create_bank_account)."
                    ),
                },
                "file_name": {
                    "type": "string",
                    "description": "Originaler Dateiname (.csv, .xml, .sta, .mt940, .940).",
                },
                "mime_type": {
                    "type": "string",
                    "description": "MIME-Type, z. B. text/csv oder application/xml.",
                },
                "content_base64": {"type": "string", "description": "Dateiinhalt als Base64."},
            },
            "required": ["company_id", "bank_account_id", "file_name", "content_base64"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/import",
        arg_location="json",
    ),
    ToolSpec(
        name="reassign_bank_transaction",
        description=(
            "Hängt einen einzelnen Bankumsatz auf ein anderes Bankkonto um (z. B. wenn "
            "beim Import das falsche Konto gewählt wurde). Eine bereits erzeugte "
            "Buchung bleibt unverändert (GoBD); standardmäßig wird der Saldo per "
            "automatischer Umgliederungsbuchung (altes an neues Konto) mitgezogen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "integer", "description": "ID des Bankumsatzes."},
                "bank_account_id": {
                    "type": "integer",
                    "description": "ID des Ziel-Bankkontos (siehe list_bank_accounts).",
                },
                "reclassify": {
                    "type": "boolean",
                    "description": (
                        "Standard true: Saldo verbuchter Umsätze per "
                        "Umgliederungsbuchung mitziehen."
                    ),
                },
            },
            "required": ["transaction_id", "bank_account_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/{transaction_id}/bank-account",
        arg_location="json",
    ),
    ToolSpec(
        name="move_bank_transactions",
        description=(
            "Hängt mehrere Bankumsätze auf ein anderes Bankkonto um: entweder alle "
            "Umsätze eines Quellkontos (source_bank_account_id, optional auf einzelne "
            "Status eingeschränkt) oder eine Liste einzelner Umsätze "
            "(transaction_ids). Genau eines von beiden angeben. Bereits erzeugte "
            "Buchungen bleiben unverändert (GoBD); standardmäßig wird der Saldo der "
            "verbuchten Umsätze per automatischer Umgliederungsbuchung mitgezogen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "target_bank_account_id": {
                    "type": "integer",
                    "description": "ID des Ziel-Bankkontos.",
                },
                "source_bank_account_id": {
                    "type": "integer",
                    "description": "ID des Quell-Bankkontos (alternativ zu transaction_ids).",
                },
                "transaction_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "IDs einzelner Bankumsätze (alternativ zu source_bank_account_id)."
                    ),
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: nur Umsätze dieser Status bewegen (open, matched, booked)."
                    ),
                },
                "reclassify": {
                    "type": "boolean",
                    "description": (
                        "Standard true: Saldo verbuchter Umsätze per "
                        "Umgliederungsbuchung mitziehen."
                    ),
                },
            },
            "required": ["company_id", "target_bank_account_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/reassign",
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
        description=(
            "Verbucht einen offenen Bankumsatz gegen ein Gegenkonto, optional mit dem "
            "Geschäftspartner der Buchung (Bank- und Gegenkontozeile)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "transaction_id": {"type": "integer", "description": "ID des Bankumsatzes."},
                "contra_account_id": {"type": "integer", "description": "Gegenkonto."},
                "tax_code_id": {"type": "integer", "description": "Optionaler Steuercode."},
                "description": {"type": "string", "description": "Optionaler Buchungstext."},
                "cost_center_id": {
                    "type": "integer",
                    "description": "Optionale Kostenstelle des Gegenkontos.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Optionales Profitcenter des Gegenkontos.",
                },
                "partner_id": {
                    "type": "integer",
                    "description": (
                        "Optionaler Geschäftspartner der Buchung (z. B. der Lieferant einer "
                        "Kartenzahlung); bei einem Sammelkonto als Gegenkonto muss die Rolle "
                        "passen."
                    ),
                },
                "partner_number": {
                    "type": "string",
                    "description": "Alternativ zu partner_id: Debitoren-/Kreditorennummer.",
                },
            },
            "required": ["transaction_id", "contra_account_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/bank-transactions/{transaction_id}/book",
        arg_location="json",
    ),
    ToolSpec(
        name="list_fints_connections",
        description=(
            "Listet die FinTS-Bankzugänge einer Gesellschaft (nur Stammdaten, keine PIN)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch deaktivierte Zugänge einschließen.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/fints-connections",
        arg_location="query",
    ),
    ToolSpec(
        name="create_fints_connection",
        description=(
            "Legt einen FinTS/HBCI-Bankzugang an (BLZ, Login, FinTS-URL). Ohne fints_url "
            "wird die URL per BLZ aus der importierten FinTS-Bankenliste ermittelt; ist "
            "eine Liste importiert, sind nur URLs aus der Liste zulässig. PIN und TAN "
            "werden nie gespeichert; sie werden erst beim Abruf übergeben."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "bank_account_id": {
                    "type": "integer",
                    "description": "ID des Bank-Sachkontos für die importierten Umsätze.",
                },
                "name": {"type": "string", "description": "Anzeigename des Zugangs."},
                "blz": {"type": "string", "description": "Bankleitzahl (8 Ziffern)."},
                "login": {"type": "string", "description": "FinTS-Benutzerkennung."},
                "fints_url": {
                    "type": "string",
                    "description": (
                        "FinTS-Endpunkt der Bank (https://…); optional, wenn die BLZ in "
                        "der FinTS-Bankenliste genau eine PIN/TAN-URL hat."
                    ),
                },
                "sepa_iban": {
                    "type": "string",
                    "description": "Optional: IBAN, falls der Zugang mehrere Konten umfasst.",
                },
            },
            "required": [
                "company_id",
                "bank_account_id",
                "name",
                "blz",
                "login",
            ],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fints-connections",
        arg_location="json",
    ),
    ToolSpec(
        name="set_fints_connection_active",
        description="Aktiviert oder deaktiviert einen FinTS-Bankzugang.",
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "integer", "description": "ID des Bankzugangs."},
                "is_active": {"type": "boolean", "description": "Neuer Aktiv-Status."},
            },
            "required": ["connection_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fints-connections/{connection_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="sync_fints_transactions",
        description=(
            "Ruft Bankumsätze per FinTS direkt bei der Bank ab und importiert sie "
            "(Dedup wie beim Dateiimport). Antwort ist entweder ein Import-Report oder "
            "status=tan_required mit dialog_id — dann submit_fints_tan verwenden. "
            "Die PIN wird nur für diesen Abruf verwendet und nicht gespeichert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "integer", "description": "ID des Bankzugangs."},
                "pin": {"type": "string", "description": "Online-Banking-PIN."},
                "from_date": {
                    "type": "string",
                    "description": "Optional: Beginn (YYYY-MM-DD), Standard 30 Tage zurück.",
                },
                "to_date": {
                    "type": "string",
                    "description": "Optional: Ende (YYYY-MM-DD), Standard heute.",
                },
            },
            "required": ["connection_id", "pin"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fints-connections/{connection_id}/sync",
        arg_location="json",
    ),
    ToolSpec(
        name="submit_fints_tan",
        description=(
            "Setzt einen FinTS-Abruf fort, der eine TAN verlangt hat. Bei entkoppelten "
            "Verfahren (pushTAN-App) tan weglassen und nach der Freigabe erneut aufrufen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "dialog_id": {
                    "type": "string",
                    "description": "dialog_id aus der tan_required-Antwort.",
                },
                "pin": {"type": "string", "description": "Online-Banking-PIN (erneut)."},
                "tan": {
                    "type": "string",
                    "description": "TAN-Code; bei entkoppelten Verfahren weglassen.",
                },
            },
            "required": ["dialog_id", "pin"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fints-dialogs/{dialog_id}/tan",
        arg_location="json",
    ),
    ToolSpec(
        name="lookup_fints_institute",
        description=(
            "Schlägt eine BLZ in der FinTS-Bankenliste nach (Institut, Ort, BIC, "
            "PIN/TAN-URL) und meldet den Stand der Liste (Anzahl, Importzeitpunkt). "
            "Ohne blz nur den Stand."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "blz": {"type": "string", "description": "Bankleitzahl (8 Ziffern)."},
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/fints-institutes",
        arg_location="query",
    ),
    ToolSpec(
        name="import_fints_institutes",
        description=(
            "Ersetzt die instanzweite FinTS-Bankenliste durch die CSV der Deutschen "
            "Kreditwirtschaft (Semikolon-getrennt, Spalten BLZ/BIC/Institut/…/"
            "PIN/TAN-Zugang URL). Nur für globale Administratoren. Die Liste erhalten "
            "registrierte FinTS-Produkte; sie wird nicht mitgeliefert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content_base64": {
                    "type": "string",
                    "description": "CSV-Inhalt der Bankenliste als Base64.",
                },
            },
            "required": ["content_base64"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fints-institutes/import",
        arg_location="json",
    ),
    ToolSpec(
        name="list_open_banking_institutes",
        description=(
            "Durchsucht die Bankenliste von Enable Banking (PSD2/Open Banking) für Banken "
            "ohne FinTS, z. B. Finom. Der zurückgegebene Name wird beim Anlegen des "
            "Zugangs als aspsp_name verwendet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "country": {"type": "string", "description": "ISO-Ländercode, Standard DE."},
                "q": {"type": "string", "description": "Optional: Namensteil, z. B. Finom."},
                "psu_type": {
                    "type": "string",
                    "enum": ["business", "personal"],
                    "description": "Optional: nur Banken mit diesem Kontotyp.",
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/open-banking/institutes",
        arg_location="query",
    ),
    ToolSpec(
        name="list_open_banking_connections",
        description=(
            "Listet die Open-Banking-Zugänge (PSD2 über Enable Banking) einer Gesellschaft "
            "mit Status der Bank-Zustimmung (authorized, expired, authorization_required)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "include_inactive": {
                    "type": "boolean",
                    "description": "Auch deaktivierte Zugänge einschließen.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/open-banking-connections",
        arg_location="query",
    ),
    ToolSpec(
        name="create_open_banking_connection",
        description=(
            "Legt einen Open-Banking-Zugang (PSD2 über Enable Banking) für eine Bank ohne "
            "FinTS an, z. B. Finom. Danach start_open_banking_authorization aufrufen, "
            "damit der Kontoinhaber bei der Bank zustimmt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "bank_account_id": {
                    "type": "integer",
                    "description": "ID des Bank-Sachkontos für die importierten Umsätze.",
                },
                "name": {"type": "string", "description": "Anzeigename des Zugangs."},
                "aspsp_name": {
                    "type": "string",
                    "description": "Bankname exakt laut list_open_banking_institutes.",
                },
                "aspsp_country": {"type": "string", "description": "ISO-Ländercode der Bank."},
                "psu_type": {
                    "type": "string",
                    "enum": ["business", "personal"],
                    "description": "Kontotyp, Standard business.",
                },
                "iban": {
                    "type": "string",
                    "description": "Optional: IBAN, falls die Zustimmung mehrere Konten umfasst.",
                },
            },
            "required": ["company_id", "bank_account_id", "name", "aspsp_name", "aspsp_country"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-banking-connections",
        arg_location="json",
    ),
    ToolSpec(
        name="set_open_banking_connection_active",
        description=(
            "Aktiviert oder deaktiviert einen Open-Banking-Zugang; beim Deaktivieren wird "
            "die Bank-Sitzung bei Enable Banking widerrufen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "integer", "description": "ID des Zugangs."},
                "is_active": {"type": "boolean", "description": "Neuer Aktiv-Status."},
            },
            "required": ["connection_id", "is_active"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-banking-connections/{connection_id}/active",
        arg_location="json",
    ),
    ToolSpec(
        name="start_open_banking_authorization",
        description=(
            "Startet die Bank-Zustimmung für einen Open-Banking-Zugang. Liefert "
            "authorization_url — der Kontoinhaber öffnet sie im Browser und stimmt bei "
            "der Bank zu. Der Rücksprung landet auf der Bankseite der Instanz; alternativ "
            "complete_open_banking_authorization mit der Rücksprung-URL aufrufen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "integer", "description": "ID des Zugangs."},
                "valid_days": {
                    "type": "integer",
                    "description": "Optional: Gültigkeit in Tagen (Standard: Maximum der Bank).",
                },
            },
            "required": ["connection_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-banking-connections/{connection_id}/authorize",
        arg_location="json",
    ),
    ToolSpec(
        name="complete_open_banking_authorization",
        description=(
            "Schließt eine Bank-Zustimmung ab, wenn der Rücksprung nicht über die "
            "Weboberfläche lief: redirect_url (komplette Rücksprung-URL) oder code + state."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "redirect_url": {"type": "string", "description": "Rücksprung-URL der Bank."},
                "code": {"type": "string", "description": "Autorisierungscode."},
                "state": {"type": "string", "description": "State aus dem Rücksprung."},
            },
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-banking/complete",
        arg_location="json",
    ),
    ToolSpec(
        name="sync_open_banking_transactions",
        description=(
            "Ruft gebuchte Bankumsätze per PSD2 (Enable Banking) ab und importiert sie "
            "(Dedup wie beim Dateiimport). Setzt eine gültige Bank-Zustimmung voraus. "
            "Ohne Datum: ab letztem Abruf (7 Tage Überlappung), beim ersten Abruf ab dem "
            "Tag nach dem letzten vorhandenen Umsatz des Bankkontos, sonst 90 Tage."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "connection_id": {"type": "integer", "description": "ID des Zugangs."},
                "from_date": {"type": "string", "description": "Optional: Beginn (YYYY-MM-DD)."},
                "to_date": {"type": "string", "description": "Optional: Ende (YYYY-MM-DD)."},
            },
            "required": ["connection_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-banking-connections/{connection_id}/sync",
        arg_location="json",
    ),
    ToolSpec(
        name="list_open_items",
        description=(
            "Listet offene Posten einer Gesellschaft; optional inklusive ausgeglichener "
            "Posten oder nur überfällige."
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
                "overdue": {
                    "type": "boolean",
                    "description": "Nur offene Posten mit Fälligkeit vor heute.",
                    "default": False,
                },
                "q": {
                    "type": "string",
                    "description": "Volltextsuche in Referenz und Gegenpartei.",
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
        name="set_company_bank_details",
        description=(
            "Hinterlegt die Auftraggeber-Bankverbindung (IBAN, optional BIC) einer "
            "Gesellschaft für SEPA-Zahlläufe; leere Werte löschen die Angabe."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "iban": {"type": "string", "description": "IBAN der Gesellschaft."},
                "bic": {"type": "string", "description": "Optional: BIC."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/companies/{company_id}/bank-details",
        arg_location="json",
    ),
    ToolSpec(
        name="list_payment_run_proposals",
        description=(
            "Offene Kreditoren-Posten mit hinterlegter Empfänger-IBAN, die in einen "
            "SEPA-Zahllauf aufgenommen werden können (fällige zuerst)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/payment-runs/proposals",
        arg_location="query",
    ),
    ToolSpec(
        name="create_sepa_payment_run",
        description=(
            "Erzeugt eine SEPA-Überweisungsdatei (pain.001.001.03) für die gewählten "
            "offenen Kreditoren-Posten. Antwort enthält xml_base64 zum Speichern; die "
            "Posten bleiben offen und werden über den Kontoauszug ausgeglichen. Der "
            "Zahllauf wird im Audit-Log protokolliert."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "open_item_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "IDs der zu zahlenden offenen Posten.",
                },
                "execution_date": {
                    "type": "string",
                    "description": "Ausführungsdatum (JJJJ-MM-TT, Standard heute).",
                },
            },
            "required": ["company_id", "open_item_ids"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/payment-runs",
        arg_location="json",
    ),
    ToolSpec(
        name="list_dunning_proposals",
        description=(
            "Mahnvorschläge: überfällige offene Forderungen mit aktueller Mahnstufe "
            "und vorgeschlagener nächster Stufe (1 = Zahlungserinnerung, 2 = 1. "
            "Mahnung, 3 = letzte Mahnung). Posten mit Höchststufe oder Mahnung in "
            "den letzten 7 Tagen werden nicht erneut vorgeschlagen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/dunning-proposals",
        arg_location="query",
    ),
    ToolSpec(
        name="record_dunning",
        description=(
            "Setzt die Mahnstufe eines überfälligen debitorischen Postens (Standard: "
            "nächste Stufe, max. 3) und protokolliert die Mahnung im Audit-Log."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "open_item_id": {"type": "integer", "description": "ID des offenen Postens."},
                "level": {
                    "type": "integer",
                    "description": "Optional: explizite Mahnstufe (1-3).",
                },
                "dunning_date": {
                    "type": "string",
                    "description": "Optional: Mahndatum (JJJJ-MM-TT, Standard heute).",
                },
            },
            "required": ["open_item_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/open-items/{open_item_id}/dunning",
        arg_location="json",
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
                "counterparty_iban": {
                    "type": "string",
                    "description": "Optional: IBAN der Gegenpartei (für SEPA-Zahlläufe).",
                },
                "counterparty_bic": {
                    "type": "string",
                    "description": "Optional: BIC der Gegenpartei.",
                },
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
                    "description": (
                        "Optional verknüpfte Ausgleichsbuchung; wird sie storniert, "
                        "lebt der Posten um den Ausgleichsbetrag wieder auf."
                    ),
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
        description=(
            "Schließt ein Geschäftsjahr ab (Vorjahre müssen abgeschlossen sein): bucht "
            "den Ergebnisvortrag in die Abschlussperiode, den Saldovortrag der "
            "Bestandskonten als EB-Werte ins Folgejahr (wird bei Bedarf angelegt) und "
            "sperrt alle Perioden — in einer Transaktion. Antwort enthält "
            "carryforward_entry_id und opening_balance_entry_id."
        ),
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
        name="verify_audit_log_integrity",
        description=(
            "Prüft Sequenzen, Vorgänger-Hashes und Inhalts-Hashes der Audit-Kette. "
            "Tenant-gebundene Tokens werden automatisch auf ihren Mandanten begrenzt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "integer",
                    "description": "Optionale Mandanten-ID für globale Zugriffe.",
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/audit-log/integrity",
        arg_location="query",
    ),
    ToolSpec(
        name="verify_compliance_integrity",
        description=(
            "Prüft Inhalts-Hashes festgeschriebener Buchungen, Belegdateien und "
            "Audit-Hashketten in einem gemeinsamen Integritätsnachweis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tenant_id": {
                    "type": "integer",
                    "description": "Optionale Mandanten-ID für globale Zugriffe.",
                },
                "company_id": {
                    "type": "integer",
                    "description": "Optionale ID einer Gesellschaft.",
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/integrity",
        arg_location="query",
    ),
    ToolSpec(
        name="get_vat_return",
        description=(
            "Berechnet die Kennziffern der Umsatzsteuer-Voranmeldung (UStVA) für einen "
            "Meldezeitraum aus den Journaldaten, ohne sie zu speichern. Kennziffern: "
            "Kz 81/86 (Bemessungsgrundlagen 19 %/7 %, volle Euro), Kz 41/42/43/44 "
            "(ig. Lieferungen, Ausfuhren), Kz 48 (steuerfrei ohne Vorsteuerabzug), Kz 21/45 "
            "(nicht steuerbar: EU-Leistungen § 18b, übrige), Kz 60, Kz 89/93 (ig. Erwerb), "
            "Kz 46/47 und 84/85 (§ 13b), Kz 66/61/67 (Vorsteuer), Kz 83 (Zahllast/Überschuss). "
            "Erträge ohne Umsatzsteuer ordnet die DATEV-Kontenfunktion zu (z. B. 8125 → 41, "
            "8336 → 21); 'warnings' und 'unassigned' nennen, was keiner Kennzahl zugeordnet ist."
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
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/vat-return",
        arg_location="query",
    ),
    ToolSpec(
        name="get_zm_report",
        description=(
            "Zusammenfassende Meldung (ZM) für einen Meldezeitraum: EU-Umsätze je "
            "Kunden-USt-IdNr. und Art (L = ig. Lieferung, D = Dreiecksgeschäft, "
            "S = sonstige Leistung), Beträge in Cent und vollen Euro. 'missing' listet "
            "ZM-relevante Erlöse ohne EU-USt-IdNr. des Kunden (Partner auf der "
            "Debitorenzeile zuordnen). Übermittlung an das BZSt erfolgt außerhalb."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "period": {
                    "type": "string",
                    "description": (
                        "Meldezeitraum: Monat 'JJJJ-MM' oder Quartal 'JJJJ-Qn'. "
                        "Alternativ date_from/date_to."
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
        path="/zm",
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
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id", "period"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/vat-returns",
        arg_location="json",
    ),
    ToolSpec(
        name="get_vat_annual_return",
        description=(
            "Berechnet die Kennziffern der USt-Jahreserklärung für ein Kalenderjahr "
            "aus den Journaldaten, ohne sie zu speichern."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "year": {"type": "integer", "description": "Kalenderjahr, z. B. 2026."},
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id", "year"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/vat-annual-return",
        arg_location="query",
    ),
    ToolSpec(
        name="create_vat_annual_return",
        description=(
            "Hält die USt-Jahreserklärung für ein Kalenderjahr als unveränderlichen "
            "Kennziffern-Snapshot fest."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "year": {"type": "integer", "description": "Kalenderjahr, z. B. 2026."},
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id", "year"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/vat-annual-returns",
        arg_location="json",
    ),
    ToolSpec(
        name="preview_income_tax_return",
        description=(
            "Berechnet Körperschaftsteuer oder Gewerbesteuer für ein Wirtschaftsjahr "
            "inklusive manueller Hinzurechnungen, Kürzungen, Verlustvortrag und "
            "Vorauszahlungen, ohne einen Snapshot zu speichern."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "year": {
                    "type": "integer",
                    "description": "Endjahr des Wirtschaftsjahres, z. B. 2026.",
                },
                "fiscal_year_id": {
                    "type": "integer",
                    "description": "Optional die eindeutige ID des Wirtschaftsjahres.",
                },
                "tax_type": {
                    "type": "string",
                    "description": "corporate_income oder trade_tax.",
                },
                "declaration_type": {
                    "type": "string",
                    "description": "declaration oder prepayment_adjustment.",
                    "default": "declaration",
                },
                "additions": {
                    "type": "array",
                    "description": "Hinzurechnungen als {code,label,amount}.",
                    "items": {"type": "object"},
                },
                "reductions": {
                    "type": "array",
                    "description": "Kürzungen als {code,label,amount}.",
                    "items": {"type": "object"},
                },
                "loss_carryforward": {
                    "type": "string",
                    "description": "Verlustvortrag.",
                    "default": "0",
                },
                "prepayments": {
                    "type": "string",
                    "description": "Bereits geleistete Vorauszahlungen.",
                    "default": "0",
                },
                "municipality_multiplier": {
                    "type": "string",
                    "description": "Gewerbesteuer-Hebesatz in Prozent.",
                },
                "trade_tax_allowance": {
                    "type": "string",
                    "description": "Gewerbesteuer-Freibetrag, bei Kapitalgesellschaften 0.",
                    "default": "0",
                },
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id", "tax_type"],
            "anyOf": [{"required": ["year"]}, {"required": ["fiscal_year_id"]}],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/income-tax-returns/preview",
        arg_location="json",
    ),
    ToolSpec(
        name="create_income_tax_return",
        description=(
            "Hält Körperschaftsteuer oder Gewerbesteuer für ein Wirtschaftsjahr als "
            "Erklärungs- oder Vorauszahlungs-Snapshot fest."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "year": {
                    "type": "integer",
                    "description": "Endjahr des Wirtschaftsjahres, z. B. 2026.",
                },
                "fiscal_year_id": {
                    "type": "integer",
                    "description": "Optional die eindeutige ID des Wirtschaftsjahres.",
                },
                "tax_type": {
                    "type": "string",
                    "description": "corporate_income oder trade_tax.",
                },
                "declaration_type": {
                    "type": "string",
                    "description": "declaration oder prepayment_adjustment.",
                    "default": "declaration",
                },
                "additions": {"type": "array", "items": {"type": "object"}},
                "reductions": {"type": "array", "items": {"type": "object"}},
                "loss_carryforward": {"type": "string", "default": "0"},
                "prepayments": {"type": "string", "default": "0"},
                "municipality_multiplier": {"type": "string"},
                "trade_tax_allowance": {"type": "string", "default": "0"},
                "include_closing_entries": _include_closing_entries_property(),
            },
            "required": ["company_id", "tax_type"],
            "anyOf": [{"required": ["year"]}, {"required": ["fiscal_year_id"]}],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/income-tax-returns",
        arg_location="json",
    ),
    ToolSpec(
        name="list_income_tax_returns",
        description="Listet festgehaltene KSt-/GewSt-Snapshots einer Gesellschaft.",
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "tax_type": {
                    "type": "string",
                    "description": "Optional: corporate_income oder trade_tax.",
                },
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/income-tax-returns",
        arg_location="query",
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
        name="get_elster_submission_summary",
        description=(
            "Zählt ELSTER-Übermittlungen einer Gesellschaft nach Status, "
            "Transport und Umgebung."
        ),
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/elster/submissions/summary",
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
                "download_link": _download_link_property(),
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/elster/submissions/{submission_id}/payload.xml",
        arg_location="query",
    ),
    ToolSpec(
        name="retry_elster_submission",
        description="Versucht eine fehlgeschlagene ELSTER-Übermittlung erneut.",
        input_schema={
            "type": "object",
            "properties": {
                "submission_id": {
                    "type": "integer",
                    "description": "ID der fehlgeschlagenen ELSTER-Übermittlung.",
                },
            },
            "required": ["submission_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/elster/submissions/{submission_id}/retry",
        arg_location="json",
    ),
    ToolSpec(
        name="get_elster_readiness",
        description=(
            "Prüft ELSTER-Umgebung, Mock-Transport, ERiC-Bibliothek, Zertifikat "
            "und lokalen ERiC-Command."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/elster/readiness",
        arg_location="none",
    ),
    ToolSpec(
        name="submit_vat_return_elster",
        description=(
            "Übermittelt eine festgehaltene UStVA über die ELSTER-Transportkante. "
            "Unterstützt transport=mock in environment=test und transport=eric "
            "über den konfigurierten lokalen ERiC-Command."
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
                    "description": "mock oder eric.",
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
        name="preflight_vat_return_elster",
        description=(
            "Prüft eine festgehaltene UStVA für die ELSTER-Übermittlung, ohne eine "
            "Submission anzulegen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vat_return_id": {"type": "integer", "description": "ID der UStVA."},
                "environment": {
                    "type": "string",
                    "description": "test oder production.",
                    "default": "test",
                },
                "transport": {
                    "type": "string",
                    "description": "mock oder eric.",
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
        path="/elster/ustva/preflight",
        arg_location="json",
    ),
    ToolSpec(
        name="submit_vat_annual_return_elster",
        description=(
            "Übermittelt eine festgehaltene USt-Jahreserklärung über die "
            "ELSTER-Transportkante."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vat_return_id": {
                    "type": "integer",
                    "description": "ID des Jahres-Snapshots.",
                },
                "environment": {
                    "type": "string",
                    "description": "test oder production; aktuell mock nur mit test.",
                    "default": "test",
                },
                "transport": {
                    "type": "string",
                    "description": "mock oder eric.",
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
        path="/elster/ust-jahreserklaerung/submit",
        arg_location="json",
    ),
    ToolSpec(
        name="preflight_vat_annual_return_elster",
        description=(
            "Prüft eine festgehaltene USt-Jahreserklärung für die ELSTER-Übermittlung, "
            "ohne eine Submission anzulegen."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "vat_return_id": {
                    "type": "integer",
                    "description": "ID des Jahres-Snapshots.",
                },
                "environment": {
                    "type": "string",
                    "description": "test oder production.",
                    "default": "test",
                },
                "transport": {
                    "type": "string",
                    "description": "mock oder eric.",
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
        path="/elster/ust-jahreserklaerung/preflight",
        arg_location="json",
    ),
    ToolSpec(
        name="get_payroll_readiness",
        description=(
            "Prueft Payroll-Compliance-Voraussetzungen fuer PAP, ELStAM und "
            "DEUEV/SV-Runner."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        http_method="GET",
        path="/payroll/readiness",
        arg_location="none",
    ),
    ToolSpec(
        name="create_payroll_employee",
        description=(
            "Legt Mitarbeiter-Stammdaten fuer das Lohnbuchhaltungs-MVP an. "
            "Abzuege werden ueber konfigurierbare Raten gerechnet; amtliche "
            "ELStAM/PAP-/DEUEV-Berechnung ist nicht enthalten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "employee_number": {"type": "string", "description": "Personalnummer."},
                "first_name": {"type": "string", "description": "Vorname."},
                "last_name": {"type": "string", "description": "Nachname."},
                "employment_start": {
                    "type": "string",
                    "description": "Eintrittsdatum JJJJ-MM-TT.",
                },
                "birth_date": {
                    "type": "string",
                    "description": "Geburtsdatum JJJJ-MM-TT fuer PAP/ELStAM.",
                },
                "tax_id": {"type": "string", "description": "Steuerliche Identifikationsnummer."},
                "tax_class": {"type": "integer", "description": "Steuerklasse 1 bis 6."},
                "child_allowances": {
                    "type": "string",
                    "description": "Kinderfreibetraege, z. B. '1.0'.",
                },
                "federal_state": {
                    "type": "string",
                    "description": "Bundesland-Kuerzel fuer Kirchensteuer, z. B. NW.",
                },
                "main_employment": {
                    "type": "boolean",
                    "description": "Hauptarbeitgeber ja/nein.",
                },
                "gross_monthly_salary": {
                    "type": "string",
                    "description": "Monatliches Bruttogehalt, z. B. '4000.00'.",
                },
                "wage_tax_rate": {
                    "type": "string",
                    "description": "Konfigurierbare Lohnsteuer-Rate, z. B. '0.20'.",
                },
                "employee_social_security_rate": {
                    "type": "string",
                    "description": "Konfigurierbare Arbeitnehmer-SV-Rate.",
                },
                "employer_social_security_rate": {
                    "type": "string",
                    "description": "Konfigurierbare Arbeitgeber-SV-Rate.",
                },
                "wage_expense_account_code": {"type": "string", "description": "Lohnaufwand."},
                "employer_social_security_expense_account_code": {
                    "type": "string",
                    "description": "Arbeitgeber-SV-Aufwand.",
                },
                "payroll_liability_account_code": {
                    "type": "string",
                    "description": "Netto-Lohnverbindlichkeit.",
                },
                "wage_tax_liability_account_code": {
                    "type": "string",
                    "description": "Lohnsteuer-Verbindlichkeit.",
                },
                "social_security_liability_account_code": {
                    "type": "string",
                    "description": "Sozialversicherungs-Verbindlichkeit.",
                },
                "cost_center_id": {
                    "type": "integer",
                    "description": "Standard-Kostenstelle für Aufwandsbuchungen.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Standard-Profitcenter für Aufwandsbuchungen.",
                },
            },
            "required": [
                "company_id",
                "employee_number",
                "first_name",
                "last_name",
                "employment_start",
                "gross_monthly_salary",
            ],
            "additionalProperties": True,
        },
        http_method="POST",
        path="/payroll/employees",
        arg_location="json",
    ),
    ToolSpec(
        name="list_payroll_employees",
        description="Listet Mitarbeiter-Stammdaten einer Gesellschaft.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/payroll/employees",
        arg_location="query",
    ),
    ToolSpec(
        name="create_payroll_run",
        description=(
            "Erzeugt einen Lohnlauf-Entwurf fuer aktive Mitarbeiter. Mit auto_post=true "
            "wird direkt eine FiBu-Journalbuchung erzeugt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "period_label": {"type": "string", "description": "Zeitraum JJJJ-MM."},
                "payment_date": {"type": "string", "description": "Zahlungsdatum JJJJ-MM-TT."},
                "employee_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional: nur diese Mitarbeiter einbeziehen.",
                },
                "auto_post": {"type": "boolean", "description": "Direkt buchen."},
            },
            "required": ["company_id", "period_label", "payment_date"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/payroll/runs",
        arg_location="json",
    ),
    ToolSpec(
        name="list_payroll_runs",
        description="Listet Lohnlaeufe einer Gesellschaft inklusive Mitarbeiterzeilen.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/payroll/runs",
        arg_location="query",
    ),
    ToolSpec(
        name="post_payroll_run",
        description="Bucht einen Lohnlauf-Entwurf als FiBu-Journalbuchung.",
        input_schema={
            "type": "object",
            "properties": {
                "payroll_run_id": {"type": "integer", "description": "ID des Lohnlaufs."},
            },
            "required": ["payroll_run_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/payroll/runs/{payroll_run_id}/post",
        arg_location="json",
    ),
    ToolSpec(
        name="create_fixed_asset",
        description=(
            "Legt ein Anlagegut in der Anlagenbuchhaltung an und wählt das AfA-Verfahren. "
            "method: 'linear' (lineare AfA, zeitanteilig im Zugangsjahr), 'degressive' "
            "(geometrisch-degressiv mit Übergang zur linearen AfA, benötigt degressive_rate), "
            "'leistung' (Leistungs-AfA, benötigt total_units), 'gwg' (Sofortabschreibung "
            "≤ 800 €), 'sammelposten' (Poolabschreibung über 5 Jahre), 'digital' "
            "(digitale Wirtschaftsgüter lt. BMF v. 22.02.2022: Nutzungsdauer 1 Jahr, "
            "volle AfA im Zugangsjahr, ohne Betragsgrenze) oder 'manuell'. "
            "Anlage- und Abschreibungskonto per interner ID oder Kontonummer "
            "(z. B. SKR03 '0400'/'4830', SKR04 '0630'/'6220')."
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
                    "enum": [
                        "linear",
                        "degressive",
                        "leistung",
                        "gwg",
                        "sammelposten",
                        "manuell",
                        "digital",
                    ],
                    "description": "AfA-Verfahren.",
                },
                "useful_life_months": {
                    "type": "integer",
                    "description": "Nutzungsdauer in Monaten (für linear/degressiv).",
                },
                "degressive_rate": {
                    "type": "string",
                    "description": (
                        "Degressiver Prozentsatz p. a. mit bis zu 4 Nachkommastellen, "
                        "z. B. '20' oder '23.0769' (nur degressive)."
                    ),
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
                    "description": "Kontonummer des Anlagekontos, z. B. '0400' (SKR04: '0630').",
                },
                "depreciation_account_code": {
                    "type": "string",
                    "description": (
                        "Kontonummer des Abschreibungskontos, z. B. '4830' (SKR04: '6220')."
                    ),
                },
                "cost_center_id": {
                    "type": "integer",
                    "description": "Standard-Kostenstelle für AfA-Aufwand.",
                },
                "profit_center_id": {
                    "type": "integer",
                    "description": "Standard-Profitcenter für AfA-Aufwand.",
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
    ToolSpec(
        name="list_tax_codes",
        description=(
            "Listet die Steuercodes einer Gesellschaft (Code, Satz, Steuerkonto). "
            "Steuercodes ordnen Buchungszeilen der UStVA zu."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/tax-codes",
        arg_location="query",
    ),
    ToolSpec(
        name="create_tax_code",
        description=(
            "Legt einen Steuercode an. Das Steuerkonto per interner ID "
            "(vat_account_id) oder Kontonummer (vat_account_code, z. B. '3806'). "
            "kind legt die Richtung fest: 'input' = Vorsteuer (Bemessungsgrundlage "
            "auf Aufwands-/Anlagenkonten, Kz 66), 'output' = Umsatzsteuer "
            "(Erlöskonten, Kz 81/86/48); ohne Angabe wird sie aus Steuerkonto "
            "bzw. Kürzel abgeleitet."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "code": {"type": "string", "description": "Kürzel, z. B. 'USt19'."},
                "rate": {"type": "string", "description": "Steuersatz, z. B. '19.00'."},
                "kind": {
                    "type": "string",
                    "enum": ["input", "output"],
                    "description": "Richtung: input = Vorsteuer, output = Umsatzsteuer.",
                },
                "description": {"type": "string"},
                "vat_account_id": {"type": "integer"},
                "vat_account_code": {"type": "string"},
            },
            "required": ["company_id", "code", "rate"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/tax-codes",
        arg_location="json",
    ),
    ToolSpec(
        name="ensure_default_tax_codes",
        description=(
            "Legt fehlende Standard-Steuercodes (USt19/USt7/VSt19/VSt7/frei) für "
            "eine Gesellschaft an und verknüpft bestehende Codes ohne Steuerkonto "
            "mit dem passenden Konto des Kontenrahmens (SKR03/SKR04). Idempotent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
            },
            "required": ["company_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/tax-codes/defaults",
        arg_location="json",
    ),
    ToolSpec(
        name="update_fixed_asset",
        description=(
            "Korrigiert Stammdaten eines Anlageguts (Bezeichnung, Anschaffungskosten, "
            "AfA-Verfahren inkl. degressivem Prozentsatz, Nutzungsdauer, Gesamtleistung, "
            "Inbetriebnahmedatum, Notizen). Plan-Parameter sind nur änderbar, solange "
            "keine AfA gebucht wurde; die Hauptbuch-Korrektur erfolgt separat als "
            "reguläre Buchung."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
                "name": {"type": "string", "description": "Neue Bezeichnung."},
                "acquisition_cost": {
                    "type": "string",
                    "description": "Korrigierte Anschaffungskosten (z. B. '1250.00').",
                },
                "method": {
                    "type": "string",
                    "description": "Neues AfA-Verfahren (nur solange keine AfA gebucht).",
                    "enum": [
                        "linear",
                        "degressive",
                        "leistung",
                        "gwg",
                        "sammelposten",
                        "manuell",
                        "digital",
                    ],
                },
                "useful_life_months": {
                    "type": "integer",
                    "description": (
                        "Neue Nutzungsdauer in Monaten (nur solange keine AfA gebucht)."
                    ),
                },
                "degressive_rate": {
                    "type": "string",
                    "description": (
                        "Degressiver Prozentsatz p. a., z. B. '20' (nur degressive; "
                        "nur solange keine AfA gebucht)."
                    ),
                },
                "total_units": {
                    "type": "string",
                    "description": (
                        "Gesamtleistung (nur leistung), z. B. '100000' "
                        "(nur solange keine AfA gebucht)."
                    ),
                },
                "in_service_date": {
                    "type": "string",
                    "description": (
                        "Neues Inbetriebnahmedatum = AfA-Beginn (JJJJ-MM-TT; "
                        "nur solange keine AfA gebucht)."
                    ),
                },
                "notes": {"type": "string", "description": "Neue Notizen."},
            },
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        http_method="PATCH",
        path="/fixed-assets/{asset_id}",
        arg_location="json",
    ),
    ToolSpec(
        name="cancel_fixed_asset",
        description=(
            "Storniert ein fälschlich angelegtes Anlagegut ohne Abgangsbuchung "
            "(z. B. retournierte oder weiterverkaufte Käufe). Nur möglich, solange "
            "keine AfA gebucht wurde; die Hauptbuch-Korrektur erfolgt separat."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "asset_id": {"type": "integer", "description": "ID des Anlageguts."},
                "reason": {"type": "string", "description": "Optionaler Stornogrund."},
            },
            "required": ["asset_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/fixed-assets/{asset_id}/cancel",
        arg_location="json",
    ),
    ToolSpec(
        name="list_chat_conversations",
        description="Listet die KI-Chat-Unterhaltungen einer Gesellschaft.",
        input_schema=_company_id_schema(),
        http_method="GET",
        path="/chat/conversations",
        arg_location="query",
    ),
    ToolSpec(
        name="get_chat_conversation",
        description="Liest eine KI-Chat-Unterhaltung inklusive aller Nachrichten.",
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "integer",
                    "description": "ID der Unterhaltung.",
                }
            },
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        http_method="GET",
        path="/chat/conversations/{conversation_id}",
        arg_location="query",
    ),
    ToolSpec(
        name="send_chat_message",
        description=(
            "Sendet eine Nachricht an den integrierten KI-Chat (LLM mit "
            "OpenBuchhaltung-Tool-Zugriff) und liefert die Antwort. Ohne "
            "conversation_id wird eine neue Unterhaltung angelegt."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "ID der Gesellschaft."},
                "conversation_id": {
                    "type": "integer",
                    "description": "ID einer bestehenden Unterhaltung (optional).",
                },
                "message": {"type": "string", "description": "Die Nachricht an den Assistenten."},
                "attachments": {
                    "type": "array",
                    "description": "Optionale Anhänge (PDF/PNG/JPG/TXT/CSV/MD).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_name": {"type": "string"},
                            "content_base64": {"type": "string"},
                        },
                        "required": ["file_name", "content_base64"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["company_id", "message"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/chat/messages",
        arg_location="json",
        timeout=LLM_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="delete_chat_conversation",
        description="Löscht eine KI-Chat-Unterhaltung samt Nachrichten.",
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "integer",
                    "description": "ID der Unterhaltung.",
                }
            },
            "required": ["conversation_id"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/chat/conversations/{conversation_id}/delete",
        arg_location="json",
    ),
    ToolSpec(
        name="rename_chat_conversation",
        description="Benennt eine KI-Chat-Unterhaltung um (höchstens 80 Zeichen).",
        input_schema={
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "integer",
                    "description": "ID der Unterhaltung.",
                },
                "title": {"type": "string", "description": "Neuer Titel."},
            },
            "required": ["conversation_id", "title"],
            "additionalProperties": False,
        },
        http_method="POST",
        path="/chat/conversations/{conversation_id}/rename",
        arg_location="json",
    ),
    ToolSpec(
        name="confirm_chat_action",
        description=(
            "Bestätigt die wartende schreibende Aktion (pending_action) einer "
            "KI-Chat-Antwort, führt sie mit den Rechten des Aufrufers aus und "
            "setzt die Unterhaltung fort."
        ),
        input_schema=_chat_action_schema(),
        http_method="POST",
        path="/chat/actions/{message_id}/confirm",
        arg_location="json",
        timeout=LLM_TOOL_TIMEOUT,
    ),
    ToolSpec(
        name="reject_chat_action",
        description=(
            "Lehnt die wartende schreibende Aktion einer KI-Chat-Antwort ab; "
            "die Unterhaltung wird mit der Ablehnung fortgesetzt."
        ),
        input_schema=_chat_action_schema(),
        http_method="POST",
        path="/chat/actions/{message_id}/reject",
        arg_location="json",
    ),
    ToolSpec(
        name="search_knowledge",
        description=(
            "Schlägt Fachwissen nach: durchsucht die mitgelieferte Wissensbasis (Umsatzsteuer, "
            "Buchführung/GoBD/Aufbewahrung, Anlagevermögen/AfA, Betriebsausgaben, Lohn und "
            "Sozialversicherung, Ertragsteuern und Fristen, Kontenrahmen SKR03/SKR04, "
            "Buchungsmuster, Arbeitsweise in OpenBuchhaltung) und liefert die passendsten "
            "Abschnitte samt Rechtsstand. Mit Fachbegriffen suchen (z. B. 'GWG Grenze', "
            "'Reverse Charge buchen', 'Aufbewahrungsfrist Rechnungen'); ohne query das "
            "Inhaltsverzeichnis."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Suchbegriffe (Fachbegriffe, Paragraf, Kontonummer).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Anzahl Treffer (1–8, Standard 3).",
                },
            },
            "additionalProperties": False,
        },
        http_method="GET",
        path="/knowledge",
        arg_location="query",
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def _decode_body(raw: bytes, headers) -> str:
    """Antworttext im Zeichensatz des Content-Type (DATEV-Export: Windows-1252)."""
    charset = (headers.get_content_charset() if headers is not None else None) or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


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
        token = current_api_token_override() or self.token
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            timeout = _REQUEST_TIMEOUT.get() or self.timeout
            with urlopen(request, timeout=timeout) as response:
                body = _decode_body(response.read(), response.headers)
                content_type = response.headers.get("Content-Type", "")
                status = response.status
        except HTTPError as exc:
            body = _decode_body(exc.read(), exc.headers)
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
            payload = _with_public_download_url(response.json)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        else:
            text = response.text
        return _tool_result(text, is_error=not response.ok)

    def _request_for_tool(self, tool: ToolSpec, arguments: dict[str, Any]) -> ApiResponse:
        # Pfad-Platzhalter wie {asset_id} aus den Argumenten füllen; die dafür
        # verbrauchten Werte nicht zusätzlich als Query/Body senden.
        path = tool.path
        remaining = _coerce_to_schema(arguments, tool.input_schema)
        if "{" in path:
            for key in list(remaining):
                placeholder = "{" + key + "}"
                if placeholder in path:
                    path = path.replace(placeholder, str(remaining.pop(key)))

        reset_timeout = _REQUEST_TIMEOUT.set(tool.timeout)
        try:
            if tool.arg_location == "query":
                return self.http.call(tool.http_method, path, params=remaining)
            if tool.arg_location == "json":
                return self.http.call(tool.http_method, path, json_body=remaining)
            return self.http.call(tool.http_method, path)
        finally:
            _REQUEST_TIMEOUT.reset(reset_timeout)


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


def _coerce_to_schema(value: Any, schema: dict[str, Any]) -> Any:
    """Gibt Zahlen in Textfeldern als Text weiter (z. B. PLZ '63628').

    Manche MCP-Clients verlieren Typ-Unions wie ["string", "null"] und senden
    ziffernartige Texte dann als JSON-Zahl, die die API ablehnt.
    """
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        return {
            key: _coerce_to_schema(item, properties[key]) if key in properties else item
            for key, item in value.items()
        }
    if isinstance(value, list):
        items = schema.get("items")
        return [_coerce_to_schema(item, items) for item in value] if items else value
    declared = schema.get("type")
    types = set(declared) if isinstance(declared, list) else {declared}
    if (
        "string" in types
        and not types & {"number", "integer"}
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return str(value)
    return value


def _with_public_download_url(payload: Any) -> Any:
    """``download_url`` auf die öffentliche Adresse des MCP-Endpunkts setzen.

    Die API kennt nur die Adresse, unter der der MCP-Server sie intern aufruft
    (z. B. ``http://app:8000``); ein Link dorthin ist für den Benutzer wertlos.
    """
    base_url = _PUBLIC_BASE_URL.get()
    if not base_url or not isinstance(payload, dict):
        return payload
    download_path = payload.get("download_path")
    if not isinstance(download_path, str) or not download_path.startswith("/"):
        return payload
    return {**payload, "download_url": f"{base_url.rstrip('/')}{download_path}"}


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
