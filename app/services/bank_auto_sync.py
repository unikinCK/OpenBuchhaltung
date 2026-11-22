"""Täglicher automatischer Umsatzabruf eingerichteter Bankzugänge (FinTS, Open Banking, MCP).

Je Zugang schaltet ein Benutzer den Abruf ein (Tabelle ``bank_auto_sync``). Der Dienst
``flask bank-auto-sync --loop`` (Compose-Dienst ``bank-sync``) ruft ab ``BANK_AUTO_SYNC_TIME``
(Europe/Berlin) einmal am Tag alle eingeschalteten Zugänge ab, die heute noch nicht dran
waren — ein Neustart oder Redeploy holt einen verpassten Lauf also nach. Die Umsätze laufen
durch dieselbe Import-Pipeline wie der Handabruf (Dedup, Audit-Log, Push-Mitteilung).

FinTS: Ohne Benutzer geht der Abruf nur mit gespeicherter PIN. Sie wird nur auf
ausdrücklichen Wunsch hinterlegt, Fernet-verschlüsselt (Schlüssel aus ``SECRET_KEY``) und nie
ausgegeben. Verlangt die Bank eine TAN oder meldet sie einen Fehler, hält der Abruf sofort an:
Jeder weitere Versuch mit falscher PIN brächte den Zugang der Sperre näher, und ein täglicher
pushTAN-Auftrag ohne Gegenüber wäre lästig. Ein erfolgreicher Handabruf setzt ihn fort und
übernimmt dabei die eingegebene PIN. Reine Verbindungsfehler (Bank nicht erreichbar) halten
nicht an — dabei wurde keine PIN geprüft.

Open Banking: nutzt die gespeicherte Bank-Sitzung. Läuft die Zustimmung ab, meldet der Abruf
das, bis sie erneuert ist.

MCP: Wallet des Bank-MCP-Servers → Bankkonto, mit dem Zugang des Benutzers, der den Abruf
eingeschaltet hat (persönlicher Zugang), bzw. dem zentralen Zugang.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.fints_sync import FinTSSyncError, cancel_pending_dialog, start_fints_sync
from app.services.mcp_bank_sync import (
    McpBankSyncError,
    ServerCaller,
    ToolCaller,
    list_wallets,
    resolve_bank_account,
    resolve_server,
    sync_mcp_bank_transactions,
)
from app.services.open_banking import (
    OpenBankingError,
    open_banking_config,
    sync_open_banking_transactions,
)
from domain.models import (
    BankAutoSync,
    Company,
    FinTSConnection,
    OpenBankingConnection,
)

logger = logging.getLogger(__name__)

SOURCE_FINTS = "fints"
SOURCE_OPEN_BANKING = "open_banking"
SOURCE_MCP = "mcp"
SOURCE_LABELS = {
    SOURCE_FINTS: "FinTS",
    SOURCE_OPEN_BANKING: "Open Banking",
    SOURCE_MCP: "MCP-Server",
}

STATUS_OK = "ok"
STATUS_TAN_REQUIRED = "tan_required"
STATUS_ERROR = "error"
STATUS_PAUSED = "paused"
STATUS_LABELS = {
    STATUS_OK: "erfolgreich",
    STATUS_TAN_REQUIRED: "TAN verlangt",
    STATUS_ERROR: "Fehler",
    STATUS_PAUSED: "angehalten",
}

PAUSE_TAN_REQUIRED = "tan_required"
PAUSE_BANK_ERROR = "bank_error"
PAUSE_PIN_MISSING = "pin_missing"
PAUSE_LABELS = {
    PAUSE_TAN_REQUIRED: "Bank verlangt eine TAN",
    PAUSE_BANK_ERROR: "Fehler der Bank",
    PAUSE_PIN_MISSING: "PIN fehlt",
}

AUDIT_ENTITY = "bank_auto_sync"
SYNC_TIMEZONE = ZoneInfo("Europe/Berlin")
DEFAULT_SYNC_TIME = "06:00"
# Abstand der Prüfungen im Dauerbetrieb: holt neu eingeschaltete Zugänge und verpasste
# Läufe zeitnah nach, ohne die Datenbank zu belasten.
CHECK_INTERVAL_SECONDS = 300
FINTS_FIRST_SYNC_DAYS = 30
# Mehr als 90 Tage zurück verlangen Banken nach PSD2 eine TAN.
FINTS_MAX_SYNC_DAYS = 89
RESYNC_OVERLAP_DAYS = 7
MESSAGE_LIMIT = 1000
_HKDF_INFO = b"openbuchhaltung:bank-auto-sync-pin:v1"
# Die Rücksprung-URL braucht nur die Bank-Zustimmung, nicht der Umsatzabruf.
_UNUSED_REDIRECT_URL = "https://localhost/bank/open-banking/rueckkehr"


class BankAutoSyncError(ValueError):
    """Der automatische Abruf lässt sich so nicht einrichten oder ausführen."""


@dataclass(slots=True)
class AutoSyncRun:
    """Ergebnis eines Abrufs für einen Zugang."""

    auto_sync_id: int
    company_id: int
    name: str
    status: str
    imported: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_sync_id": self.auto_sync_id,
            "company_id": self.company_id,
            "name": self.name,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "imported": self.imported,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Konfiguration und Verschlüsselung
# ---------------------------------------------------------------------------


def sync_time(config: Mapping[str, Any] | None) -> time:
    """Uhrzeit (Europe/Berlin), ab der der tägliche Abruf läuft."""
    raw = str((config or {}).get("BANK_AUTO_SYNC_TIME") or DEFAULT_SYNC_TIME).strip()
    try:
        hour, minute = (int(part) for part in raw.split(":"))
        return time(hour, minute)
    except ValueError as exc:
        raise BankAutoSyncError(
            f"BANK_AUTO_SYNC_TIME „{raw}“ ist keine Uhrzeit im Format HH:MM."
        ) from exc


def _fernet(secret_key: str | None) -> Fernet:
    if not secret_key:
        raise BankAutoSyncError("SECRET_KEY fehlt – die PIN kann nicht verschlüsselt werden.")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        str(secret_key).encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def _encrypt_pin(pin: str, secret_key: str | None) -> str:
    return _fernet(secret_key).encrypt(pin.encode("utf-8")).decode("ascii")


def _decrypt_pin(token: str | None, secret_key: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("Gespeicherte FinTS-PIN nicht entschlüsselbar (SECRET_KEY geändert?).")
        return None


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def display_name(entry: BankAutoSync) -> str:
    if entry.source == SOURCE_FINTS and entry.fints_connection is not None:
        return entry.fints_connection.name
    if entry.source == SOURCE_OPEN_BANKING and entry.open_banking_connection is not None:
        return entry.open_banking_connection.name
    if entry.source == SOURCE_MCP:
        server = entry.mcp_server.name if entry.mcp_server is not None else "MCP-Server"
        return f"{server} · {entry.mcp_wallet_name or entry.mcp_wallet_id}"
    return f"Zugang {entry.id}"


def serialize_auto_sync(entry: BankAutoSync) -> dict[str, Any]:
    """Für UI und API; die PIN selbst verlässt den Server nie."""
    account = entry.bank_account
    return {
        "id": entry.id,
        "company_id": entry.company_id,
        "source": entry.source,
        "source_label": SOURCE_LABELS.get(entry.source, entry.source),
        "name": display_name(entry),
        "bank_account_id": entry.bank_account_id,
        "bank_account": f"{account.code} – {account.name}" if account is not None else None,
        "fints_connection_id": entry.fints_connection_id,
        "open_banking_connection_id": entry.open_banking_connection_id,
        "mcp_server_id": entry.mcp_server_id,
        "mcp_wallet_id": entry.mcp_wallet_id,
        "pin_stored": bool(entry.secret_encrypted) if entry.source == SOURCE_FINTS else None,
        "user": entry.user.username if entry.user is not None else None,
        "is_active": entry.is_active,
        "paused": entry.paused_reason is not None,
        "paused_reason": entry.paused_reason,
        "paused_label": PAUSE_LABELS.get(entry.paused_reason or ""),
        "last_run_at": _iso(entry.last_run_at),
        "last_status": entry.last_status,
        "last_status_label": STATUS_LABELS.get(entry.last_status or ""),
        "last_message": entry.last_message,
        "last_imported": entry.last_imported,
        "last_synced_to": _iso(entry.last_synced_to),
        "created_by": entry.created_by,
        "created_at": _iso(entry.created_at),
    }


def list_bank_auto_syncs(
    session: Session, *, company_id: int, include_inactive: bool = False
) -> list[BankAutoSync]:
    query = select(BankAutoSync).where(BankAutoSync.company_id == company_id)
    if not include_inactive:
        query = query.where(BankAutoSync.is_active.is_(True))
    return list(session.execute(query.order_by(BankAutoSync.id)).scalars())


def auto_sync_for_fints(session: Session, connection_id: int) -> BankAutoSync | None:
    return session.execute(
        select(BankAutoSync).where(BankAutoSync.fints_connection_id == connection_id)
    ).scalar_one_or_none()


def auto_sync_for_open_banking(session: Session, connection_id: int) -> BankAutoSync | None:
    return session.execute(
        select(BankAutoSync).where(BankAutoSync.open_banking_connection_id == connection_id)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Einschalten, ausschalten, fortsetzen
# ---------------------------------------------------------------------------


def _audit(session: Session, entry: BankAutoSync, action: str, changed_by: str, **payload) -> None:
    log_audit_event(
        session=session,
        tenant_id=entry.tenant_id,
        company_id=entry.company_id,
        entity_type=AUDIT_ENTITY,
        entity_id=str(entry.id),
        action=action,
        changed_by=changed_by,
        payload={"source": entry.source, "name": display_name(entry), **payload},
    )


def _activate(
    session: Session, entry: BankAutoSync, *, user_id: int | None, changed_by: str
) -> BankAutoSync:
    if entry.id is None:
        session.add(entry)
    entry.is_active = True
    entry.paused_reason = None
    entry.user_id = user_id
    session.flush()
    _audit(session, entry, "enabled", changed_by, bank_account_id=entry.bank_account_id)
    session.commit()
    return entry


def enable_fints_auto_sync(
    session: Session,
    *,
    connection_id: int,
    pin: str,
    secret_key: str | None,
    user_id: int | None,
    changed_by: str,
) -> BankAutoSync:
    """Schaltet den täglichen Abruf eines FinTS-Zugangs ein und hinterlegt dafür die PIN."""
    connection = session.get(FinTSConnection, connection_id)
    if connection is None:
        raise BankAutoSyncError("Bankzugang nicht gefunden.")
    if not connection.is_active:
        raise BankAutoSyncError("Der Bankzugang ist deaktiviert.")
    pin = (pin or "").strip()
    if not pin:
        raise BankAutoSyncError("Für den automatischen Abruf ist die PIN nötig.")
    entry = auto_sync_for_fints(session, connection.id) or BankAutoSync(
        tenant_id=connection.tenant_id,
        company_id=connection.company_id,
        source=SOURCE_FINTS,
        fints_connection_id=connection.id,
        created_by=changed_by,
    )
    entry.bank_account_id = connection.bank_account_id
    entry.secret_encrypted = _encrypt_pin(pin, secret_key)
    return _activate(session, entry, user_id=user_id, changed_by=changed_by)


def enable_open_banking_auto_sync(
    session: Session, *, connection_id: int, user_id: int | None, changed_by: str
) -> BankAutoSync:
    """Schaltet den täglichen Abruf eines Open-Banking-Zugangs ein."""
    connection = session.get(OpenBankingConnection, connection_id)
    if connection is None:
        raise BankAutoSyncError("Open-Banking-Zugang nicht gefunden.")
    if not connection.is_active:
        raise BankAutoSyncError("Der Open-Banking-Zugang ist deaktiviert.")
    entry = auto_sync_for_open_banking(session, connection.id) or BankAutoSync(
        tenant_id=connection.tenant_id,
        company_id=connection.company_id,
        source=SOURCE_OPEN_BANKING,
        open_banking_connection_id=connection.id,
        created_by=changed_by,
    )
    entry.bank_account_id = connection.bank_account_id
    return _activate(session, entry, user_id=user_id, changed_by=changed_by)


def enable_mcp_auto_sync(
    session: Session,
    *,
    company: Company,
    caller: ToolCaller,
    server_id: int,
    bank_account_id: int,
    wallet_id: str,
    user_id: int | None,
    changed_by: str,
) -> BankAutoSync:
    """Schaltet den täglichen Abruf einer Wallet eines Bank-MCP-Servers in ein Bankkonto ein.

    ``caller`` ruft den Server mit dem Zugang des Benutzers; so ist geprüft, dass die Wallet
    existiert und der Zugang funktioniert.
    """
    try:
        server = resolve_server(session, company, server_id)
        account = resolve_bank_account(session, company, bank_account_id)
        wallets = list_wallets(caller)
    except McpBankSyncError as exc:
        raise BankAutoSyncError(str(exc)) from exc
    wallet = next((item for item in wallets if item.id == wallet_id), None)
    if wallet is None:
        raise BankAutoSyncError("Die Wallet gibt es bei diesem Zugang nicht.")
    entry = session.execute(
        select(BankAutoSync).where(
            BankAutoSync.company_id == company.id,
            BankAutoSync.mcp_server_id == server.id,
            BankAutoSync.mcp_wallet_id == wallet.id,
        )
    ).scalar_one_or_none() or BankAutoSync(
        tenant_id=company.tenant_id,
        company_id=company.id,
        source=SOURCE_MCP,
        mcp_server_id=server.id,
        mcp_wallet_id=wallet.id,
        created_by=changed_by,
    )
    entry.bank_account_id = account.id
    entry.mcp_wallet_name = wallet.name[:120] or None
    return _activate(session, entry, user_id=user_id, changed_by=changed_by)


def disable_bank_auto_sync(
    session: Session, *, auto_sync_id: int, changed_by: str
) -> BankAutoSync:
    """Schaltet den Abruf aus; eine gespeicherte PIN wird dabei gelöscht."""
    entry = session.get(BankAutoSync, auto_sync_id)
    if entry is None:
        raise BankAutoSyncError("Automatischer Abruf nicht gefunden.")
    entry.is_active = False
    entry.paused_reason = None
    entry.secret_encrypted = None
    _audit(session, entry, "disabled", changed_by)
    session.commit()
    return entry


def deactivate_for_connection(
    session: Session,
    *,
    fints_connection_id: int | None = None,
    open_banking_connection_id: int | None = None,
    changed_by: str,
) -> None:
    """Ein deaktivierter Zugang ruft nicht mehr automatisch ab (und behält keine PIN).

    Committet nicht; der Aufrufer schließt die Deaktivierung des Zugangs ab.
    """
    if fints_connection_id is not None:
        entry = auto_sync_for_fints(session, fints_connection_id)
    elif open_banking_connection_id is not None:
        entry = auto_sync_for_open_banking(session, open_banking_connection_id)
    else:
        return
    if entry is None or (not entry.is_active and not entry.secret_encrypted):
        return
    entry.is_active = False
    entry.paused_reason = None
    entry.secret_encrypted = None
    _audit(session, entry, "disabled", changed_by, reason="connection_deactivated")


def after_manual_fints_sync(
    session: Session, *, connection_id: int, pin: str, secret_key: str | None, changed_by: str
) -> bool:
    """Nach einem erfolgreichen Handabruf: angehaltenen Abruf fortsetzen, PIN übernehmen.

    Liefert, ob ein angehaltener Abruf wieder läuft.
    """
    entry = auto_sync_for_fints(session, connection_id)
    pin = (pin or "").strip()
    if entry is None or not entry.is_active or not pin:
        return False
    changed = False
    if _decrypt_pin(entry.secret_encrypted, secret_key) != pin:
        entry.secret_encrypted = _encrypt_pin(pin, secret_key)
        changed = True
    resumed = entry.paused_reason is not None
    if resumed:
        _audit(session, entry, "resumed", changed_by, paused_reason=entry.paused_reason)
        entry.paused_reason = None
        changed = True
    if changed:
        session.commit()
    return resumed


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def today_local(now: datetime | None = None) -> date:
    return (now or _now()).astimezone(SYNC_TIMEZONE).date()


def _ran_today(entry: BankAutoSync, today: date) -> bool:
    last = entry.last_run_at
    if last is None:
        return False
    if last.tzinfo is None:  # SQLite liefert naive Zeitstempel (UTC)
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone(SYNC_TIMEZONE).date() >= today


def _actor(entry: BankAutoSync) -> str:
    if entry.user is not None:
        return f"{entry.user.username} (automatisch)"[:120]
    return "automatischer Bankabruf"


def _user_dict(entry: BankAutoSync) -> dict[str, Any] | None:
    user = entry.user
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
    }


def _mcp_caller(session_factory, entry: BankAutoSync, user: dict | None) -> ServerCaller:
    """Verbindung zum Bank-MCP-Server; in Tests durch einen Fake ersetzt."""
    return ServerCaller(
        session_factory=session_factory,
        server_id=entry.mcp_server_id,
        tenant_id=entry.tenant_id,
        company_id=entry.company_id,
        user=user,
    )


def _finish(
    session: Session,
    entry: BankAutoSync,
    *,
    at: datetime,
    status: str,
    message: str,
    imported: int = 0,
    synced_to: date | None = None,
    pause: str | None = None,
) -> AutoSyncRun:
    entry.last_run_at = at
    entry.last_status = status
    entry.last_message = message[:MESSAGE_LIMIT]
    entry.last_imported = imported
    if synced_to is not None and (entry.last_synced_to is None or synced_to > entry.last_synced_to):
        entry.last_synced_to = synced_to
    if pause is not None and entry.paused_reason != pause:
        entry.paused_reason = pause
        _audit(session, entry, "paused", _actor(entry), paused_reason=pause)
    session.commit()
    return AutoSyncRun(
        auto_sync_id=entry.id,
        company_id=entry.company_id,
        name=display_name(entry),
        status=status,
        imported=imported,
        message=entry.last_message,
    )


def _report_text(imported: int, duplicates: int, errors: int, prefix: str = "") -> str:
    return f"{prefix}{imported} neu, {duplicates} schon vorhanden, {errors} Fehler."


def _is_connection_problem(exc: BaseException) -> bool:
    """Bank nicht erreichbar oder Netzfehler – dabei wurde keine PIN geprüft.

    python-fints verpackt Verbindungsfehler (z. B. in ``FinTSDialogInitError``); deshalb
    zählt die ganze Ursachenkette.
    """
    import requests
    from fints.exceptions import FinTSConnectionError

    seen: set[int] = set()
    current: BaseException | None = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen:
        if isinstance(current, (requests.RequestException, FinTSConnectionError)):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _run_fints(session: Session, entry: BankAutoSync, config, at: datetime) -> AutoSyncRun:
    today = today_local(at)
    pin = _decrypt_pin(entry.secret_encrypted, config.get("SECRET_KEY"))
    if not pin:
        return _finish(
            session,
            entry,
            at=at,
            status=STATUS_ERROR,
            message="Keine lesbare PIN hinterlegt – bitte den automatischen Abruf mit PIN "
            "neu einschalten.",
            pause=PAUSE_PIN_MISSING,
        )
    if entry.last_synced_to is not None:
        from_date = entry.last_synced_to - timedelta(days=RESYNC_OVERLAP_DAYS)
    else:
        from_date = today - timedelta(days=FINTS_FIRST_SYNC_DAYS)
    from_date = min(max(from_date, today - timedelta(days=FINTS_MAX_SYNC_DAYS)), today)
    actor = _actor(entry)
    try:
        result = start_fints_sync(
            session=session,
            connection_id=entry.fints_connection_id,
            pin=pin,
            product_id=config.get("FINTS_PRODUCT_ID"),
            from_date=from_date,
            to_date=today,
            changed_by=actor,
        )
    except FinTSSyncError as exc:
        session.rollback()
        if _is_connection_problem(exc):
            return _finish(
                session,
                entry,
                at=at,
                status=STATUS_ERROR,
                message=f"Bank nicht erreichbar – neuer Versuch beim nächsten Lauf. ({exc})",
            )
        return _finish(
            session,
            entry,
            at=at,
            status=STATUS_ERROR,
            message=f"{exc} Der automatische Abruf ist angehalten, damit Fehlversuche den "
            "Zugang nicht sperren. Ein erfolgreicher Handabruf setzt ihn fort und übernimmt "
            "die dabei eingegebene PIN; alternativ die PIN neu hinterlegen.",
            pause=PAUSE_BANK_ERROR,
        )
    if result.challenge is not None:
        cancel_pending_dialog(
            session=session, dialog_id=result.challenge.dialog_id, changed_by=actor
        )
        return _finish(
            session,
            entry,
            at=at,
            status=STATUS_TAN_REQUIRED,
            message="Die Bank verlangt eine TAN. Einmal von Hand abrufen (Bankseite → "
            "FinTS-Direktabruf) – danach läuft der automatische Abruf weiter.",
            pause=PAUSE_TAN_REQUIRED,
        )
    report = result.report
    return _finish(
        session,
        entry,
        at=at,
        status=STATUS_OK if report.error_rows == 0 else STATUS_ERROR,
        message=_report_text(
            report.imported_rows,
            report.duplicate_rows,
            report.error_rows,
            prefix=f"{from_date:%d.%m.%Y}–{today:%d.%m.%Y}: ",
        ),
        imported=report.imported_rows,
        synced_to=today,
    )


def _run_open_banking(
    session: Session, entry: BankAutoSync, config, at: datetime
) -> AutoSyncRun:
    today = today_local(at)
    try:
        report = sync_open_banking_transactions(
            session=session,
            connection_id=entry.open_banking_connection_id,
            config=open_banking_config(config, fallback_redirect_url=_UNUSED_REDIRECT_URL),
            secret_key=config.get("SECRET_KEY"),
            to_date=today,
            changed_by=_actor(entry),
        )
    except OpenBankingError as exc:
        session.rollback()
        return _finish(session, entry, at=at, status=STATUS_ERROR, message=str(exc))
    return _finish(
        session,
        entry,
        at=at,
        status=STATUS_OK if report.error_rows == 0 else STATUS_ERROR,
        message=_report_text(report.imported_rows, report.duplicate_rows, report.error_rows),
        imported=report.imported_rows,
        synced_to=today,
    )


def _run_mcp(session: Session, session_factory, entry: BankAutoSync, at: datetime) -> AutoSyncRun:
    today = today_local(at)
    company = session.get(Company, entry.company_id)
    if entry.user_id is not None and (entry.user is None or not entry.user.is_active):
        return _finish(
            session,
            entry,
            at=at,
            status=STATUS_ERROR,
            message="Der Benutzer, der den Abruf eingeschaltet hat, ist deaktiviert – bitte "
            "den Abruf neu einschalten.",
        )
    try:
        server = resolve_server(session, company, entry.mcp_server_id)
        with _mcp_caller(session_factory, entry, _user_dict(entry)) as caller:
            result = sync_mcp_bank_transactions(
                session=session,
                caller=caller,
                company=company,
                server=server,
                bank_account_id=entry.bank_account_id,
                wallet_id=entry.mcp_wallet_id,
                to_date=today,
                changed_by=_actor(entry),
            )
    except McpBankSyncError as exc:
        session.rollback()
        return _finish(session, entry, at=at, status=STATUS_ERROR, message=str(exc))
    report = result.report
    return _finish(
        session,
        entry,
        at=at,
        status=STATUS_OK if report.error_rows == 0 else STATUS_ERROR,
        message=_report_text(
            report.imported_rows,
            result.known + report.duplicate_rows,
            report.error_rows,
            prefix=f"{result.from_date:%d.%m.%Y}–{result.to_date:%d.%m.%Y}: ",
        ),
        imported=report.imported_rows,
        synced_to=result.to_date,
    )


def _run_entry(session: Session, session_factory, entry: BankAutoSync, config, at: datetime):
    if entry.source == SOURCE_FINTS:
        return _run_fints(session, entry, config, at)
    if entry.source == SOURCE_OPEN_BANKING:
        return _run_open_banking(session, entry, config, at)
    return _run_mcp(session, session_factory, entry, at)


def run_bank_auto_syncs(
    session_factory: Callable[[], Session],
    *,
    config: Mapping[str, Any],
    company_id: int | None = None,
    auto_sync_id: int | None = None,
    only_due: bool = False,
    now: datetime | None = None,
) -> list[AutoSyncRun]:
    """Ruft die eingeschalteten, nicht angehaltenen Zugänge ab – jeden in eigener Sitzung.

    ``only_due``: nur Zugänge, die heute (Europe/Berlin) noch nicht abgerufen wurden.
    """
    at = now or _now()
    today = today_local(at)
    with session_factory() as session:
        query = select(BankAutoSync).where(
            BankAutoSync.is_active.is_(True), BankAutoSync.paused_reason.is_(None)
        )
        if company_id is not None:
            query = query.where(BankAutoSync.company_id == company_id)
        if auto_sync_id is not None:
            query = query.where(BankAutoSync.id == auto_sync_id)
        entries = list(session.execute(query.order_by(BankAutoSync.id)).scalars())
        ids = [entry.id for entry in entries if not (only_due and _ran_today(entry, today))]

    runs: list[AutoSyncRun] = []
    for entry_id in ids:
        with session_factory() as session:
            entry = session.get(BankAutoSync, entry_id)
            if entry is None or not entry.is_active or entry.paused_reason is not None:
                continue
            try:
                run = _run_entry(session, session_factory, entry, config, at)
            except Exception:  # ein Zugang darf die anderen nie aufhalten
                logger.exception("Automatischer Bankabruf %s fehlgeschlagen", entry_id)
                session.rollback()
                entry = session.get(BankAutoSync, entry_id)
                run = _finish(
                    session,
                    entry,
                    at=at,
                    status=STATUS_ERROR,
                    message="Unerwarteter Fehler beim Abruf – Details im Server-Log.",
                )
            logger.info(
                "Automatischer Bankabruf %s (%s): %s – %s",
                run.auto_sync_id,
                run.name,
                run.status,
                run.message,
            )
            runs.append(run)
    return runs


def due_now(config: Mapping[str, Any], now: datetime | None = None) -> bool:
    """Ist die tägliche Abrufzeit heute schon erreicht?"""
    local = (now or _now()).astimezone(SYNC_TIMEZONE)
    return local.time() >= sync_time(config)
