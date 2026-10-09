"""KI-Zugang (LLM) je Benutzer und Auflösung des wirksamen LLM-Endpoints.

Ein Administrator hinterlegt pro Benutzer einen API-Key. Standard ist OpenAI
(``https://api.openai.com/v1/responses``); alternativ lässt sich ein eigener
OpenAI-``/responses``-kompatibler Endpoint angeben (z. B. Azure OpenAI,
OpenRouter, ein lokales Ollama/vLLM). Der Key wird mit Fernet verschlüsselt
gespeichert — der Schlüssel ist aus ``SECRET_KEY`` abgeleitet — und nie wieder
ausgegeben, nur seine letzten vier Zeichen.

Alle LLM-Funktionen (KI-Chat, Beleg-OCR, KI-Kontrolle, Belegabgleich,
Dokument-Update) lösen ihren Endpoint über :func:`resolve_llm_endpoint` auf:
Hat der handelnde Benutzer einen KI-Zugang, gilt dieser; sonst die
Instanz-Endpoints aus der Umgebung (``*_LLM_ENDPOINT_URL``, ohne API-Key —
gedacht für lokale bzw. per Proxy abgesicherte Endpoints).
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app, g, has_request_context

from domain.models import User

logger = logging.getLogger(__name__)

PROVIDER_OPENAI = "openai"
PROVIDER_CUSTOM = "custom"
PROVIDERS = (PROVIDER_OPENAI, PROVIDER_CUSTOM)
PROVIDER_LABELS = {
    PROVIDER_OPENAI: "OpenAI",
    PROVIDER_CUSTOM: "Eigener Endpunkt",
}

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_LLM_MODEL = "gpt-4.1-mini"

MAX_ENDPOINT_URL_LENGTH = 500
MAX_MODEL_LENGTH = 120
MAX_API_KEY_LENGTH = 1000

# Verwendungszwecke und ihre Instanz-Konfiguration (Endpoint, Modell).
PURPOSE_CHAT = "chat"
PURPOSE_DOCUMENT = "document"
PURPOSE_RECEIPT_OCR = "receipt_ocr"
PURPOSE_RECEIPT = "receipt"
PURPOSE_RECEIPT_MATCH = "receipt_match"
_PURPOSE_CONFIG = {
    PURPOSE_CHAT: ("CHAT_LLM_ENDPOINT_URL", "CHAT_LLM_MODEL"),
    PURPOSE_DOCUMENT: ("DOCUMENT_LLM_ENDPOINT_URL", "DOCUMENT_LLM_MODEL"),
    PURPOSE_RECEIPT_OCR: ("RECEIPT_OCR_ENDPOINT_URL", "RECEIPT_OCR_MODEL"),
    PURPOSE_RECEIPT: ("RECEIPT_LLM_ENDPOINT_URL", "RECEIPT_LLM_MODEL"),
    PURPOSE_RECEIPT_MATCH: ("RECEIPT_MATCH_LLM_ENDPOINT_URL", "RECEIPT_MATCH_LLM_MODEL"),
}

SOURCE_USER = "user"
SOURCE_INSTANCE = "instance"

_HKDF_INFO = b"openbuchhaltung:user-llm-api-key:v1"


class LlmSettingsError(ValueError):
    """Ungültige Eingabe für den KI-Zugang eines Benutzers."""


@dataclass(frozen=True, slots=True)
class LlmEndpoint:
    """Wirksamer LLM-Endpoint für einen Aufruf."""

    url: str
    model: str
    api_key: str | None = None
    source: str = SOURCE_INSTANCE


def llm_headers(api_key: str | None) -> dict[str, str]:
    """HTTP-Header für einen LLM-Aufruf (JSON, optional Bearer-Key)."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ---------------------------------------------------------------------------
# Verschlüsselung
# ---------------------------------------------------------------------------


def _fernet(secret_key: str | None = None) -> Fernet:
    secret = secret_key if secret_key is not None else current_app.config.get("SECRET_KEY")
    if not secret:
        raise LlmSettingsError("SECRET_KEY fehlt – API-Keys können nicht verschlüsselt werden.")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        str(secret).encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_api_key(api_key: str, *, secret_key: str | None = None) -> str:
    return _fernet(secret_key).encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(token: str | None, *, secret_key: str | None = None) -> str | None:
    """Entschlüsselt einen gespeicherten Key; ``None`` wenn nicht lesbar.

    Nicht lesbar wird ein Key, wenn sich ``SECRET_KEY`` seit dem Speichern
    geändert hat — dann muss der Administrator ihn neu eintragen.
    """
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning(
            "Gespeicherter LLM-API-Key ist nicht entschlüsselbar (SECRET_KEY geändert?)."
        )
        return None


# ---------------------------------------------------------------------------
# Pflege durch den Administrator
# ---------------------------------------------------------------------------


def normalize_endpoint_url(raw: str | None) -> str:
    """Prüft eine Endpoint-URL; ``…/v1`` wird zu ``…/v1/responses`` ergänzt."""
    url = (raw or "").strip()
    if not url:
        raise LlmSettingsError("Für einen eigenen Endpunkt ist die Endpunkt-URL Pflicht.")
    if len(url) > MAX_ENDPOINT_URL_LENGTH:
        raise LlmSettingsError("Die Endpunkt-URL ist zu lang.")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise LlmSettingsError("Die Endpunkt-URL muss mit http:// oder https:// beginnen.")
    if parts.username or parts.password:
        raise LlmSettingsError(
            "Zugangsdaten gehören nicht in die URL – bitte als API-Key eintragen."
        )
    if parts.path.rstrip("/").endswith("/v1"):
        url = url.split("?", 1)[0].rstrip("/") + "/responses"
    return url


def _clean_api_key(raw: str | None) -> str | None:
    api_key = (raw or "").strip()
    if not api_key:
        return None
    if len(api_key) > MAX_API_KEY_LENGTH:
        raise LlmSettingsError("Der API-Key ist zu lang.")
    if not api_key.isascii() or not api_key.isprintable() or any(c.isspace() for c in api_key):
        raise LlmSettingsError("Der API-Key enthält unzulässige Zeichen.")
    return api_key


def _clean_model(raw: str | None) -> str | None:
    model = (raw or "").strip()
    if not model:
        return None
    if len(model) > MAX_MODEL_LENGTH:
        raise LlmSettingsError("Der Modellname ist zu lang.")
    return model


def apply_user_llm_settings(
    user: User,
    *,
    provider: str | None,
    endpoint_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> None:
    """Setzt den KI-Zugang eines Benutzers (Aufrufer committet).

    Ein leerer ``api_key`` lässt einen bereits gespeicherten Key unverändert.
    OpenAI braucht einen Key; ein eigener Endpunkt eine URL (Key optional,
    z. B. für lokale Modelle).
    """
    provider = (provider or PROVIDER_OPENAI).strip().lower()
    if provider not in PROVIDERS:
        raise LlmSettingsError("Anbieter muss 'openai' oder 'custom' sein.")
    new_api_key = _clean_api_key(api_key)
    cleaned_model = _clean_model(model)
    cleaned_url = normalize_endpoint_url(endpoint_url) if provider == PROVIDER_CUSTOM else None
    if provider == PROVIDER_OPENAI and new_api_key is None and not user.llm_api_key_encrypted:
        raise LlmSettingsError("Für OpenAI ist ein API-Key Pflicht.")

    user.llm_provider = provider
    user.llm_endpoint_url = cleaned_url
    user.llm_model = cleaned_model
    if new_api_key is not None:
        user.llm_api_key_encrypted = encrypt_api_key(new_api_key)
        user.llm_api_key_last4 = new_api_key[-4:]


def clear_user_llm_settings(user: User) -> None:
    user.llm_provider = None
    user.llm_endpoint_url = None
    user.llm_model = None
    user.llm_api_key_encrypted = None
    user.llm_api_key_last4 = None


def user_llm_summary(user: User) -> dict[str, object]:
    """Öffentliche Sicht auf den KI-Zugang (ohne Key, nur dessen letzte vier Zeichen)."""
    provider = user.llm_provider
    if provider not in PROVIDERS:
        return {"configured": False}
    return {
        "configured": True,
        "provider": provider,
        "provider_label": PROVIDER_LABELS[provider],
        "endpoint_url": OPENAI_RESPONSES_URL
        if provider == PROVIDER_OPENAI
        else user.llm_endpoint_url,
        "model": user.llm_model or DEFAULT_LLM_MODEL,
        "api_key_last4": user.llm_api_key_last4,
        # False, wenn der gespeicherte Key nach einem SECRET_KEY-Wechsel nicht
        # mehr entschlüsselbar ist und neu eingetragen werden muss.
        "api_key_readable": (
            decrypt_api_key(user.llm_api_key_encrypted) is not None
            if user.llm_api_key_encrypted
            else None
        ),
    }


def llm_settings_audit_payload(user: User) -> dict[str, object]:
    summary = user_llm_summary(user)
    summary.pop("provider_label", None)
    summary.pop("api_key_readable", None)
    return summary


# ---------------------------------------------------------------------------
# Auflösung zur Laufzeit
# ---------------------------------------------------------------------------


def current_request_user_id() -> int | None:
    """ID des handelnden Benutzers (API-Token/OAuth, interne Aufrufe oder UI-Session)."""
    if not has_request_context():
        return None
    from app.auth import current_user  # Zyklus auth -> services vermeiden

    user = getattr(g, "api_user", None) or current_user()
    return user.get("id") if isinstance(user, dict) else None


def _user_endpoint(user: User) -> LlmEndpoint | None:
    provider = user.llm_provider
    if provider == PROVIDER_OPENAI:
        url = OPENAI_RESPONSES_URL
    elif provider == PROVIDER_CUSTOM and user.llm_endpoint_url:
        url = user.llm_endpoint_url
    else:
        return None
    return LlmEndpoint(
        url=url,
        model=user.llm_model or DEFAULT_LLM_MODEL,
        api_key=decrypt_api_key(user.llm_api_key_encrypted),
        source=SOURCE_USER,
    )


def _instance_endpoint(purpose: str) -> LlmEndpoint | None:
    endpoint_key, model_key = _PURPOSE_CONFIG[purpose]
    config = current_app.config
    url = config.get(endpoint_key)
    if not url:
        return None
    return LlmEndpoint(url=url, model=config.get(model_key) or DEFAULT_LLM_MODEL)


def resolve_llm_endpoint(
    purpose: str, *, user_id: int | None = None, session=None
) -> LlmEndpoint | None:
    """Wirksamer Endpoint für ``purpose``: KI-Zugang des Benutzers vor Instanz-Default.

    ``user_id=None`` nimmt den handelnden Benutzer des laufenden Requests.
    Liefert ``None``, wenn weder Benutzer noch Instanz einen Endpoint haben.
    """
    if purpose not in _PURPOSE_CONFIG:
        raise ValueError(f"Unbekannter LLM-Verwendungszweck: {purpose}")
    if user_id is None:
        user_id = current_request_user_id()
    if user_id is not None:
        if session is not None:
            endpoint = _load_user_endpoint(session, user_id)
        else:
            with current_app.extensions["db_session_factory"]() as own_session:
                endpoint = _load_user_endpoint(own_session, user_id)
        if endpoint is not None:
            return endpoint
    return _instance_endpoint(purpose)


def _load_user_endpoint(session, user_id: int) -> LlmEndpoint | None:
    user = session.get(User, user_id)
    return _user_endpoint(user) if user is not None else None


def receipt_analysis_options(
    *, llm_purpose: str = PURPOSE_RECEIPT, user_id: int | None = None, session=None
) -> dict[str, object]:
    """Keyword-Argumente für ``analyze_document``/``create_match_suggestion``."""
    ocr = resolve_llm_endpoint(PURPOSE_RECEIPT_OCR, user_id=user_id, session=session)
    llm = resolve_llm_endpoint(llm_purpose, user_id=user_id, session=session)
    return {
        "ocr_endpoint": ocr.url if ocr else None,
        "ocr_model": ocr.model if ocr else DEFAULT_LLM_MODEL,
        "ocr_api_key": ocr.api_key if ocr else None,
        "llm_endpoint": llm.url if llm else None,
        "llm_model": llm.model if llm else DEFAULT_LLM_MODEL,
        "llm_api_key": llm.api_key if llm else None,
    }
