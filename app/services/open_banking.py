"""PSD2-Umsatzabruf über Enable Banking für Banken ohne FinTS (z. B. Finom).

Ablauf: Bankzugang anlegen (Bank laut Enable-Banking-Liste + Buchhaltungs-
Bankkonto) → Autorisierung starten (``POST /auth`` liefert die Bank-URL, der
Kontoinhaber stimmt dort per SCA zu) → Rücksprung mit ``code``/``state``
(``POST /sessions``) → Umsatzabruf (``GET /accounts/{uid}/transactions``),
solange die Zustimmung gilt (je nach Bank bis zu 180 Tage).

Authentifizierung gegenüber Enable Banking: kurzlebiges RS256-JWT, signiert
mit dem privaten Schlüssel der registrierten Anwendung
(``ENABLE_BANKING_APP_ID``/``ENABLE_BANKING_KEY_FILE``). Bank-Zugangsdaten
sieht die Anwendung nie. Gespeichert wird die Sitzungs-ID — zusammen mit dem
App-Schlüssel ist sie ein Lesezugriff auf das Konto, deshalb liegt sie
Fernet-verschlüsselt (Schlüssel aus ``SECRET_KEY`` abgeleitet) in der DB.

Hinweis Lizenz: Der kostenlose Enable-Banking-Modus deckt laut deren AGB nur
private Konten ab; Geschäftskonten erfordern einen Vertrag mit Enable Banking.

Die abgerufenen Umsätze laufen durch dieselbe Import-Pipeline wie Dateiimport
und FinTS (Dedup über Hash, Audit-Log, Status "open").
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.bank_import import BankImportReport, import_bank_items
from app.services.bank_statement import row_from_open_banking_transaction
from domain.models import Account, BankTransaction, Company, OpenBankingConnection

logger = logging.getLogger(__name__)

PROVIDER_ENABLE_BANKING = "enable_banking"
DEFAULT_API_URL = "https://api.enablebanking.com"
PSU_TYPES = ("business", "personal")
# Bank-Autorisierung muss innerhalb dieser Zeit abgeschlossen werden.
AUTH_MAX_AGE = timedelta(minutes=30)
DEFAULT_CONSENT_DAYS = 90
DEFAULT_FIRST_SYNC_DAYS = 90
# Erneuter Abruf überlappt den letzten um ein paar Tage (Nachbuchungen);
# Duplikate fängt der Dedup-Hash über die Bankreferenz ab.
RESYNC_OVERLAP_DAYS = 7
MAX_TRANSACTION_PAGES = 100
JWT_LIFETIME_SECONDS = 3600
HTTP_TIMEOUT_SECONDS = 30

_HKDF_INFO = b"openbuchhaltung:open-banking-session:v1"


class OpenBankingError(ValueError):
    """Raised when an Open-Banking operation is invalid or fails."""


@dataclass(frozen=True, slots=True)
class OpenBankingConfig:
    app_id: str
    private_key_pem: bytes
    redirect_url: str
    api_url: str = DEFAULT_API_URL


@dataclass(frozen=True, slots=True)
class AuthorizationStart:
    connection_id: int
    url: str
    valid_until: datetime


# ---------------------------------------------------------------------------
# Konfiguration


def open_banking_configured(app_config) -> bool:
    return bool(
        app_config.get("ENABLE_BANKING_APP_ID") and app_config.get("ENABLE_BANKING_KEY_FILE")
    )


def open_banking_config(
    app_config, *, fallback_redirect_url: str | None = None
) -> OpenBankingConfig:
    """Liest die Enable-Banking-Konfiguration; Fehlermeldung nennt die fehlende Variable."""
    app_id = (app_config.get("ENABLE_BANKING_APP_ID") or "").strip()
    key_file = (app_config.get("ENABLE_BANKING_KEY_FILE") or "").strip()
    if not app_id or not key_file:
        raise OpenBankingError(
            "Open Banking ist nicht konfiguriert: ENABLE_BANKING_APP_ID und "
            "ENABLE_BANKING_KEY_FILE (privater Schlüssel der Enable-Banking-Anwendung) setzen."
        )
    try:
        private_key_pem = Path(key_file).read_bytes()
    except OSError as exc:
        raise OpenBankingError(
            "ENABLE_BANKING_KEY_FILE ist nicht lesbar — Pfad und Berechtigungen prüfen."
        ) from exc

    redirect_url = (app_config.get("ENABLE_BANKING_REDIRECT_URL") or "").strip()
    redirect_url = redirect_url or (fallback_redirect_url or "")
    if not redirect_url.startswith("https://"):
        raise OpenBankingError(
            "ENABLE_BANKING_REDIRECT_URL muss eine https-URL sein (in der "
            "Enable-Banking-Anwendung als Redirect-URL hinterlegt)."
        )

    api_url = (app_config.get("ENABLE_BANKING_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
    parts = urlsplit(api_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise OpenBankingError("ENABLE_BANKING_API_URL muss eine https-URL ohne Zugangsdaten sein.")
    return OpenBankingConfig(
        app_id=app_id, private_key_pem=private_key_pem, redirect_url=redirect_url, api_url=api_url
    )


# ---------------------------------------------------------------------------
# HTTP-Client


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_jwt(config: OpenBankingConfig, *, now: int | None = None) -> str:
    """RS256-JWT für die Enable-Banking-API (``kid`` = Anwendungs-ID)."""
    try:
        key = serialization.load_pem_private_key(config.private_key_pem, password=None)
    except (ValueError, TypeError) as exc:
        raise OpenBankingError(
            "ENABLE_BANKING_KEY_FILE enthält keinen lesbaren privaten PEM-Schlüssel."
        ) from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise OpenBankingError("ENABLE_BANKING_KEY_FILE muss ein RSA-Schlüssel sein.")
    issued_at = int(time.time()) if now is None else now
    header = {"typ": "JWT", "alg": "RS256", "kid": config.app_id}
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": issued_at,
        "exp": issued_at + JWT_LIFETIME_SECONDS,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


class EnableBankingClient:
    """Minimaler JSON-Client für die Enable-Banking-API."""

    def __init__(self, config: OpenBankingConfig) -> None:
        self.config = config

    def request(self, method: str, path: str, *, params=None, body=None) -> dict:
        url = self.config.api_url + path
        if params:
            url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {build_jwt(self.config)}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                raw = response.read()
        except HTTPError as exc:
            raise _http_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            logger.warning("Enable Banking nicht erreichbar: %s", exc)
            raise OpenBankingError(
                f"Enable Banking nicht erreichbar ({type(exc).__name__})."
            ) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OpenBankingError("Enable Banking lieferte keine gültige JSON-Antwort.") from exc

    def get(self, path: str, params=None) -> dict:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body=body)

    def delete(self, path: str) -> dict:
        return self.request("DELETE", path)


class OpenBankingHttpError(OpenBankingError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def _http_error(exc: HTTPError) -> OpenBankingHttpError:
    detail = ""
    try:
        body = json.loads(exc.read() or b"{}")
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or body.get("detail") or "")
    except (ValueError, OSError):
        pass
    detail = " ".join(detail.split())[:300]
    logger.warning("Enable Banking HTTP %s: %s", exc.code, detail)
    message = f"Enable Banking meldet HTTP {exc.code}"
    return OpenBankingHttpError(f"{message}: {detail}" if detail else message + ".", exc.code)


def _build_client(config: OpenBankingConfig):
    """Erzeugt den API-Client; in Tests durch einen Fake ersetzt."""
    return EnableBankingClient(config)


# ---------------------------------------------------------------------------
# Verschlüsselung der Sitzungs-ID


def _fernet(secret_key: str | None) -> Fernet:
    if not secret_key:
        raise OpenBankingError(
            "SECRET_KEY fehlt – die Bank-Sitzung kann nicht verschlüsselt werden."
        )
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        str(secret_key).encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def _encrypt_session_id(session_id: str, secret_key: str | None) -> str:
    return _fernet(secret_key).encrypt(session_id.encode("utf-8")).decode("ascii")


def _decrypt_session_id(token: str | None, secret_key: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Open-Banking-Sitzung nicht entschlüsselbar (SECRET_KEY geändert?).")
        return None


# ---------------------------------------------------------------------------
# Bankenliste


def list_open_banking_institutes(
    *, config: OpenBankingConfig, country: str, psu_type: str | None = None, query: str = ""
) -> list[dict[str, object]]:
    """Banken eines Landes laut Enable Banking, optional nach Namensteil gefiltert."""
    country = _normalize_country(country)
    if psu_type is not None:
        psu_type = _normalize_psu_type(psu_type)
    response = _build_client(config).get(
        "/aspsps", params={"country": country, "psu_type": psu_type, "service": "AIS"}
    )
    needle = query.strip().casefold()
    institutes = []
    for aspsp in response.get("aspsps") or []:
        name = str(aspsp.get("name") or "")
        if needle and needle not in name.casefold():
            continue
        institutes.append(
            {
                "name": name,
                "country": aspsp.get("country") or country,
                "bic": aspsp.get("bic"),
                "psu_types": aspsp.get("psu_types") or [],
                "maximum_consent_validity_days": _consent_days(aspsp),
                "beta": bool(aspsp.get("beta")),
            }
        )
    return sorted(institutes, key=lambda item: str(item["name"]).casefold())


def _consent_days(aspsp: dict) -> int | None:
    seconds = aspsp.get("maximum_consent_validity")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return max(1, int(seconds // 86400))


# ---------------------------------------------------------------------------
# Verwaltung der Bankzugänge


def serialize_connection(connection: OpenBankingConnection) -> dict[str, object]:
    return {
        "id": connection.id,
        "company_id": connection.company_id,
        "bank_account_id": connection.bank_account_id,
        "name": connection.name,
        "provider": connection.provider,
        "aspsp_name": connection.aspsp_name,
        "aspsp_country": connection.aspsp_country,
        "psu_type": connection.psu_type,
        "iban": connection.iban,
        "status": connection_status(connection),
        "consent_valid_until": _iso(connection.consent_valid_until),
        "last_synced_at": _iso(connection.last_synced_at),
        "last_synced_to": connection.last_synced_to.isoformat()
        if connection.last_synced_to
        else None,
        "is_active": connection.is_active,
        "created_at": connection.created_at.isoformat(),
    }


def connection_status(connection: OpenBankingConnection) -> str:
    """``inactive``, ``authorization_required``, ``expired`` oder ``authorized``."""
    if not connection.is_active:
        return "inactive"
    if not connection.session_id_encrypted or not connection.account_uid:
        return "authorization_required"
    if _consent_expired(connection):
        return "expired"
    return "authorized"


def create_open_banking_connection(
    *,
    session: Session,
    company_id: int,
    bank_account_id: int,
    name: str,
    aspsp_name: str,
    aspsp_country: str,
    psu_type: str = "business",
    iban: str | None = None,
    changed_by: str,
) -> OpenBankingConnection:
    company = session.get(Company, company_id)
    if company is None:
        raise OpenBankingError("Gesellschaft nicht gefunden.")
    bank_account = session.get(Account, bank_account_id)
    if bank_account is None or bank_account.company_id != company.id:
        raise OpenBankingError("Bankkonto nicht gefunden.")

    name = (name or "").strip()
    aspsp_name = (aspsp_name or "").strip()
    if not name:
        raise OpenBankingError("Name des Bankzugangs fehlt.")
    if not aspsp_name:
        raise OpenBankingError("Bank (Name laut Enable-Banking-Bankenliste) fehlt.")
    aspsp_country = _normalize_country(aspsp_country)
    psu_type = _normalize_psu_type(psu_type)
    iban = (iban or "").replace(" ", "").upper() or None
    if iban is not None and not (15 <= len(iban) <= 34 and iban[:2].isalpha() and iban.isalnum()):
        raise OpenBankingError("IBAN ist ungültig.")

    existing = session.execute(
        select(OpenBankingConnection).where(
            OpenBankingConnection.company_id == company.id, OpenBankingConnection.name == name
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise OpenBankingError("Ein Open-Banking-Zugang mit diesem Namen existiert bereits.")

    connection = OpenBankingConnection(
        tenant_id=company.tenant_id,
        company_id=company.id,
        bank_account_id=bank_account.id,
        name=name,
        provider=PROVIDER_ENABLE_BANKING,
        aspsp_name=aspsp_name,
        aspsp_country=aspsp_country,
        psu_type=psu_type,
        iban=iban,
    )
    session.add(connection)
    session.flush()
    _audit(
        session,
        connection,
        "created",
        changed_by,
        {
            "name": name,
            "aspsp_name": aspsp_name,
            "aspsp_country": aspsp_country,
            "psu_type": psu_type,
            "bank_account_id": bank_account.id,
        },
    )
    session.commit()
    session.refresh(connection)
    return connection


def list_open_banking_connections(
    *, session: Session, company_id: int, include_inactive: bool = False
) -> list[OpenBankingConnection]:
    stmt = select(OpenBankingConnection).where(OpenBankingConnection.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(OpenBankingConnection.is_active.is_(True))
    return list(session.execute(stmt.order_by(OpenBankingConnection.name)).scalars())


def set_open_banking_connection_active(
    *,
    session: Session,
    connection_id: int,
    is_active: bool,
    changed_by: str,
    config: OpenBankingConfig | None = None,
    secret_key: str | None = None,
) -> OpenBankingConnection:
    """(De-)aktiviert einen Zugang; beim Deaktivieren wird die Bank-Sitzung widerrufen."""
    connection = session.get(OpenBankingConnection, connection_id)
    if connection is None:
        raise OpenBankingError("Open-Banking-Zugang nicht gefunden.")

    revoked = False
    if not is_active and connection.session_id_encrypted:
        session_id = _decrypt_session_id(connection.session_id_encrypted, secret_key)
        if session_id and config is not None:
            try:
                _build_client(config).delete(f"/sessions/{quote(session_id, safe='')}")
                revoked = True
            except OpenBankingError as exc:
                # Lokal trotzdem deaktivieren; die Zustimmung läuft bei der Bank ab.
                logger.warning("Widerruf der Open-Banking-Sitzung fehlgeschlagen: %s", exc)
        _clear_authorization(connection)

    connection.is_active = is_active
    _audit(
        session,
        connection,
        "activated" if is_active else "deactivated",
        changed_by,
        {"name": connection.name, "session_revoked": revoked},
    )
    session.commit()
    session.refresh(connection)
    return connection


# ---------------------------------------------------------------------------
# Autorisierung (Zustimmung des Kontoinhabers bei der Bank)


def start_open_banking_authorization(
    *,
    session: Session,
    connection_id: int,
    config: OpenBankingConfig,
    valid_days: int | None = None,
    changed_by: str,
) -> AuthorizationStart:
    """Startet die Bank-Zustimmung; die zurückgegebene URL öffnet der Kontoinhaber."""
    connection = _get_active_connection(session, connection_id)
    client = _build_client(config)

    aspsps = (
        client.get(
            "/aspsps", params={"country": connection.aspsp_country, "psu_type": connection.psu_type}
        ).get("aspsps")
        or []
    )
    aspsp = next((item for item in aspsps if item.get("name") == connection.aspsp_name), None)
    if aspsp is None:
        raise OpenBankingError(
            f"Bank „{connection.aspsp_name}“ ({connection.aspsp_country}) bietet Enable Banking "
            f"für Kontotyp „{connection.psu_type}“ nicht an — Namen in der Bankenliste prüfen."
        )

    max_days = _consent_days(aspsp) or DEFAULT_CONSENT_DAYS
    days = max_days if valid_days is None else int(valid_days)
    if days < 1:
        raise OpenBankingError("Gültigkeit der Zustimmung muss mindestens 1 Tag betragen.")
    days = min(days, max_days)
    valid_until = datetime.now(timezone.utc) + timedelta(days=days)

    state = secrets.token_urlsafe(32)
    response = client.post(
        "/auth",
        {
            "access": {"valid_until": valid_until.isoformat()},
            "aspsp": {"name": connection.aspsp_name, "country": connection.aspsp_country},
            "state": state,
            "redirect_url": config.redirect_url,
            "psu_type": connection.psu_type,
        },
    )
    url = response.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise OpenBankingError("Enable Banking lieferte keine Autorisierungs-URL.")

    connection.auth_state = state
    connection.auth_started_at = datetime.now(timezone.utc)
    _audit(
        session,
        connection,
        "authorization_started",
        changed_by,
        {"valid_until": valid_until.isoformat()},
    )
    session.commit()
    return AuthorizationStart(connection_id=connection.id, url=url, valid_until=valid_until)


def find_connection_by_state(session: Session, state: str) -> OpenBankingConnection | None:
    if not state:
        return None
    return session.execute(
        select(OpenBankingConnection).where(OpenBankingConnection.auth_state == state)
    ).scalar_one_or_none()


def complete_open_banking_authorization(
    *,
    session: Session,
    state: str,
    code: str,
    config: OpenBankingConfig,
    secret_key: str | None,
    changed_by: str,
) -> OpenBankingConnection:
    """Tauscht den Rücksprung-Code gegen eine Sitzung und wählt das Konto aus."""
    connection = find_connection_by_state(session, state)
    if connection is None:
        raise OpenBankingError(
            "Autorisierung nicht gefunden oder bereits abgeschlossen — bitte neu starten."
        )
    started = _aware(connection.auth_started_at)
    if started is None or datetime.now(timezone.utc) - started > AUTH_MAX_AGE:
        connection.auth_state = None
        connection.auth_started_at = None
        session.commit()
        raise OpenBankingError("Autorisierung ist abgelaufen — bitte neu starten.")
    if not code:
        raise OpenBankingError("Rücksprung der Bank ohne Autorisierungscode.")

    # state ist nur einmal verwendbar, auch wenn der Abschluss scheitert.
    connection.auth_state = None
    connection.auth_started_at = None
    session.commit()

    client = _build_client(config)
    response = client.post("/sessions", {"code": code})
    session_id = response.get("session_id")
    if not session_id:
        raise OpenBankingError("Enable Banking lieferte keine Sitzung.")

    try:
        account = _select_account(connection, response.get("accounts") or [])
    except OpenBankingError:
        _revoke_quietly(client, session_id)
        raise

    access = response.get("access") or {}
    connection.session_id_encrypted = _encrypt_session_id(str(session_id), secret_key)
    connection.account_uid = str(account["uid"])
    connection.iban = connection.iban or ((account.get("account_id") or {}).get("iban") or None)
    connection.consent_valid_until = _parse_datetime(access.get("valid_until"))
    _audit(
        session,
        connection,
        "authorized",
        changed_by,
        {"iban": connection.iban, "consent_valid_until": _iso(connection.consent_valid_until)},
    )
    session.commit()
    session.refresh(connection)
    return connection


def _select_account(connection: OpenBankingConnection, accounts: list[dict]) -> dict:
    accounts = [account for account in accounts if account.get("uid")]
    if not accounts:
        raise OpenBankingError("Die Bank hat kein Konto für den Abruf freigegeben.")
    if connection.iban:
        matching = [
            account
            for account in accounts
            if ((account.get("account_id") or {}).get("iban") or "").replace(" ", "").upper()
            == connection.iban
        ]
        if not matching:
            raise OpenBankingError(
                f"Kein freigegebenes Konto mit IBAN {connection.iban} — bei der Bank das "
                "richtige Konto auswählen."
            )
        return matching[0]
    if len(accounts) == 1:
        return accounts[0]
    ibans = ", ".join(
        str((account.get("account_id") or {}).get("iban") or account["uid"]) for account in accounts
    )
    raise OpenBankingError(
        f"Die Zustimmung umfasst mehrere Konten ({ibans}) — Zugang mit IBAN neu anlegen "
        "oder bei der Bank nur ein Konto freigeben."
    )


def _revoke_quietly(client, session_id: str) -> None:
    try:
        client.delete(f"/sessions/{quote(str(session_id), safe='')}")
    except OpenBankingError as exc:
        logger.warning("Widerruf der Open-Banking-Sitzung fehlgeschlagen: %s", exc)


# ---------------------------------------------------------------------------
# Umsatzabruf


def default_sync_range(
    session: Session, connection: OpenBankingConnection, *, today: date | None = None
) -> tuple[date, date]:
    """Standard-Zeitraum: ab letztem Abruf (mit Überlappung), sonst ab dem
    letzten bereits vorhandenen Umsatz des Bankkontos (z. B. aus CSV-Import),
    sonst die letzten 90 Tage."""
    to_date = today or date.today()
    if connection.last_synced_to is not None:
        from_date = connection.last_synced_to - timedelta(days=RESYNC_OVERLAP_DAYS)
    else:
        newest = session.execute(
            select(func.max(BankTransaction.booking_date)).where(
                BankTransaction.company_id == connection.company_id,
                BankTransaction.bank_account_id == connection.bank_account_id,
            )
        ).scalar_one_or_none()
        if newest is not None:
            # Erst ab dem Folgetag: Umsätze aus anderen Quellen tragen andere
            # Referenzen und würden sonst doppelt importiert.
            from_date = newest + timedelta(days=1)
        else:
            from_date = to_date - timedelta(days=DEFAULT_FIRST_SYNC_DAYS)
    return min(from_date, to_date), to_date


def sync_open_banking_transactions(
    *,
    session: Session,
    connection_id: int,
    config: OpenBankingConfig,
    secret_key: str | None,
    from_date: date | None = None,
    to_date: date | None = None,
    changed_by: str,
) -> BankImportReport:
    """Ruft gebuchte Umsätze ab und importiert sie in das Bankkonto des Zugangs."""
    connection = _get_active_connection(session, connection_id)
    session_id = _decrypt_session_id(connection.session_id_encrypted, secret_key)
    if not session_id or not connection.account_uid:
        raise OpenBankingError(
            f"Zugang „{connection.name}“ ist nicht autorisiert — bitte Bank-Zustimmung erteilen."
        )
    if _consent_expired(connection):
        raise OpenBankingError(
            f"Die Bank-Zustimmung für „{connection.name}“ ist abgelaufen — bitte erneuern."
        )

    default_from, default_to = default_sync_range(session, connection)
    to_date = to_date or default_to
    from_date = from_date or min(default_from, to_date)
    if from_date > to_date:
        raise OpenBankingError("Das Von-Datum darf nicht nach dem Bis-Datum liegen.")

    transactions = _fetch_transactions(
        _build_client(config), connection, from_date=from_date, to_date=to_date
    )
    items = [
        row_from_open_banking_transaction(position, transaction)
        for position, transaction in enumerate(transactions, start=1)
    ]
    report = import_bank_items(
        session=session,
        company_id=connection.company_id,
        bank_account_id=connection.bank_account_id,
        items=items,
        changed_by=changed_by,
        source="open_banking",
    )
    connection = session.get(OpenBankingConnection, connection_id)
    connection.last_synced_at = datetime.now(timezone.utc)
    if connection.last_synced_to is None or to_date > connection.last_synced_to:
        connection.last_synced_to = to_date
    session.commit()
    return report


def _fetch_transactions(
    client, connection: OpenBankingConnection, *, from_date: date, to_date: date
) -> list[dict]:
    path = f"/accounts/{quote(connection.account_uid or '', safe='')}/transactions"
    params: dict[str, str] = {"date_from": from_date.isoformat(), "date_to": to_date.isoformat()}
    booked: list[dict] = []
    for _ in range(MAX_TRANSACTION_PAGES):
        try:
            response = client.get(path, params)
        except OpenBankingHttpError as exc:
            if exc.status in (401, 403, 404):
                raise OpenBankingError(
                    f"{exc} — die Bank-Zustimmung ist vermutlich abgelaufen oder widerrufen; "
                    "bitte erneuern."
                ) from exc
            raise
        for transaction in response.get("transactions") or []:
            # Vorgemerkte Umsätze ändern sich noch (Betrag, Referenz) — nur Gebuchtes.
            if (transaction.get("status") or "BOOK").upper() == "BOOK":
                booked.append(transaction)
        continuation_key = response.get("continuation_key")
        if not continuation_key:
            return booked
        params = {**params, "continuation_key": str(continuation_key)}
    raise OpenBankingError("Zu viele Ergebnisseiten — bitte einen kürzeren Zeitraum abrufen.")


# ---------------------------------------------------------------------------
# Interna


def _get_active_connection(session: Session, connection_id: int) -> OpenBankingConnection:
    connection = session.get(OpenBankingConnection, connection_id)
    if connection is None:
        raise OpenBankingError("Open-Banking-Zugang nicht gefunden.")
    if not connection.is_active:
        raise OpenBankingError("Der Open-Banking-Zugang ist deaktiviert.")
    return connection


def _clear_authorization(connection: OpenBankingConnection) -> None:
    connection.session_id_encrypted = None
    connection.account_uid = None
    connection.consent_valid_until = None
    connection.auth_state = None
    connection.auth_started_at = None


def _consent_expired(connection: OpenBankingConnection) -> bool:
    valid_until = _aware(connection.consent_valid_until)
    return valid_until is not None and valid_until <= datetime.now(timezone.utc)


def _normalize_country(country: str) -> str:
    country = (country or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise OpenBankingError("Land muss ein zweistelliger ISO-Code sein (z. B. DE).")
    return country


def _normalize_psu_type(psu_type: str) -> str:
    psu_type = (psu_type or "").strip().lower()
    if psu_type not in PSU_TYPES:
        raise OpenBankingError("Kontotyp muss „business“ oder „personal“ sein.")
    return psu_type


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _audit(
    session: Session,
    connection: OpenBankingConnection,
    action: str,
    changed_by: str,
    payload: dict,
) -> None:
    log_audit_event(
        session=session,
        tenant_id=connection.tenant_id,
        company_id=connection.company_id,
        entity_type="open_banking_connection",
        entity_id=str(connection.id),
        action=action,
        changed_by=changed_by,
        payload=payload,
    )
