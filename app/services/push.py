"""Push-Mitteilungen an Mobilgeräte (iOS-App) über ein zentrales Push-Relay.

Ablauf (ADR-003):

1. Die App registriert ihr APNs-Gerätetoken bei der Instanz (``POST /api/v1/devices``)
   und wählt die gewünschten Ereignisse.
2. Fachereignisse legen in der Transaktion der auslösenden Änderung je passendem
   Gerät eine Zeile in der Ausgangswarteschlange (``push_notification``) an. Ohne
   konfiguriertes Relay (``PUSH_RELAY_URL``) entsteht nichts.
3. Der Dispatcher (``flask push-dispatch``, eigener Compose-Dienst) übergibt offene
   Zeilen an das Relay. Nur das Relay hält den APNs-Schlüssel; selbst betriebene
   Instanzen brauchen deshalb keinen eigenen Apple-Account.

Datensparsamkeit: Texte nennen nur Anzahlen bzw. Meldezeitraum, keine Beträge,
Gesellschafts- oder Partnernamen. Die App kann über ``company_id`` und
``server_url`` im Datenteil lokal anreichern (Notification Service Extension).

Relay-Protokoll v1: ``POST <PUSH_RELAY_URL>/v1/notifications`` mit
``Authorization: Bearer <PUSH_RELAY_TOKEN>`` und
``{"notifications": [{"id", "device_token", "environment", "title", "body",
"category", "thread_id", "data": {...}}]}``. Antwort:
``{"results": [{"id", "status": "sent" | "invalid_token" | "error", "error"}]}``.
``invalid_token`` (APNs: Gerät abgemeldet) widerruft das Gerät.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.security_events import record_security_event
from domain.models import Company, OpenItem, PushDevice, PushNotification, User

logger = logging.getLogger(__name__)

EVENT_BANK_TRANSACTIONS = "bank_transactions"
EVENT_ELSTER_SUBMISSION = "elster_submission"
EVENT_OVERDUE_OPEN_ITEMS = "overdue_open_items"
EVENT_TEST = "test"
EVENTS = {
    EVENT_BANK_TRANSACTIONS: "Neue Bankumsätze",
    EVENT_ELSTER_SUBMISSION: "ELSTER-Übermittlung",
    EVENT_OVERDUE_OPEN_ITEMS: "Überfällige offene Posten (täglich)",
}
ENVIRONMENTS = ("production", "sandbox")
PLATFORMS = ("ios",)

TITLE = "OpenBuchhaltung"
MAX_ATTEMPTS = 5
STALE_AFTER = timedelta(hours=24)
RETENTION = timedelta(days=30)
DISPATCH_BATCH = 100
DIGEST_TIMEZONE = ZoneInfo("Europe/Berlin")
DEFAULT_DIGEST_HOUR = 8
RELAY_TIMEOUT_SECONDS = 15

_DEVICE_TOKEN_RE = re.compile(r"^[0-9a-f]{64,200}$")

RelayTransport = Callable[[str, str, dict[str, Any], float], dict[str, Any]]


class PushError(ValueError):
    """Ungültige Eingaben bei Geräteregistrierung oder -pflege."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite liefert naive Zeitstempel zurück; als UTC interpretieren."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if config is not None:
        return config
    return current_app.config if has_app_context() else {}


def push_enabled(config: Mapping[str, Any] | None = None) -> bool:
    return bool((_config(config).get("PUSH_RELAY_URL") or "").strip())


def push_status(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "enabled": push_enabled(config),
        "events": [{"key": key, "label": label} for key, label in EVENTS.items()],
        "default_events": list(EVENTS),
        "environments": list(ENVIRONMENTS),
    }


# ---------------------------------------------------------------------------
# Geräte
# ---------------------------------------------------------------------------


def _validate_events(events: Any) -> list[str]:
    if events is None:
        return list(EVENTS)
    if not isinstance(events, list) or not all(isinstance(event, str) for event in events):
        raise PushError("events muss eine Liste von Ereignisschlüsseln sein.")
    unknown = sorted(set(events) - set(EVENTS))
    if unknown:
        raise PushError(f"Unbekannte Ereignisse: {', '.join(unknown)}.")
    return [event for event in EVENTS if event in events]


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def register_device(
    session: Session,
    *,
    user: User,
    device_token: Any,
    environment: Any = None,
    platform: Any = None,
    device_name: Any = None,
    app_version: Any = None,
    server_url: Any = None,
    events: Any = None,
) -> PushDevice:
    """Registriert ein Gerät oder aktualisiert es (Schlüssel: Gerätetoken).

    Meldet sich auf demselben Gerät ein anderer Benutzer an, geht das Gerät auf ihn
    über; ein widerrufenes Gerät wird wieder aktiv. Der Aufrufer committet.
    """
    token = str(device_token or "").strip().lower()
    if not _DEVICE_TOKEN_RE.match(token):
        raise PushError("device_token muss ein hexadezimales APNs-Gerätetoken sein.")
    environment = str(environment or "production")
    if environment not in ENVIRONMENTS:
        raise PushError("environment muss production oder sandbox sein.")
    platform = str(platform or "ios")
    if platform not in PLATFORMS:
        raise PushError("platform muss ios sein.")
    selected_events = _validate_events(events)

    device = session.execute(
        select(PushDevice).where(PushDevice.device_token == token)
    ).scalar_one_or_none()
    previous_user_id = device.user_id if device is not None else None
    if device is None:
        device = PushDevice(device_token=token, user_id=user.id)
        session.add(device)
    device.user_id = user.id
    device.platform = platform
    device.environment = environment
    device.events = selected_events
    device.device_name = _clean_text(device_name, 100)
    device.app_version = _clean_text(app_version, 40)
    device.server_url = _clean_text(server_url, 500)
    device.last_seen_at = _utcnow()
    device.revoked_at = None
    session.flush()
    record_security_event(
        session,
        user=user,
        action="push_device_registered",
        actor=user.username,
        payload={
            "device_id": device.id,
            "device_name": device.device_name,
            "previous_user_id": previous_user_id,
            "events": selected_events,
        },
    )
    return device


def update_device(
    session: Session,
    device: PushDevice,
    *,
    actor: str,
    events: Any = None,
    device_name: Any = None,
) -> PushDevice:
    """Ändert abonnierte Ereignisse und/oder den Gerätenamen. Der Aufrufer committet."""
    if events is not None:
        device.events = _validate_events(events)
    if device_name is not None:
        device.device_name = _clean_text(device_name, 100)
    session.flush()
    record_security_event(
        session,
        user=session.get(User, device.user_id),
        action="push_device_updated",
        actor=actor,
        payload={"device_id": device.id, "events": device.events},
    )
    return device


def revoke_device(
    session: Session, device: PushDevice, *, actor: str, reason: str | None = None
) -> None:
    """Widerruft ein Gerät; offene Mitteilungen dafür werden verworfen."""
    if device.revoked_at is None:
        device.revoked_at = _utcnow()
    session.execute(
        PushNotification.__table__.update()
        .where(
            PushNotification.device_id == device.id,
            PushNotification.status == "pending",
        )
        .values(status="discarded", last_error="Gerät widerrufen.")
    )
    record_security_event(
        session,
        user=session.get(User, device.user_id),
        action="push_device_revoked",
        actor=actor,
        payload={"device_id": device.id, "reason": reason},
    )


def device_dict(device: PushDevice) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        value = _aware(value)
        return value.isoformat() if value is not None else None

    return {
        "id": device.id,
        "user_id": device.user_id,
        "username": device.user.username if device.user else None,
        "platform": device.platform,
        "environment": device.environment,
        "device_name": device.device_name,
        "app_version": device.app_version,
        "server_url": device.server_url,
        "token_suffix": device.device_token[-8:],
        "events": list(device.events or []),
        "created_at": _iso(device.created_at),
        "last_seen_at": _iso(device.last_seen_at),
        "revoked_at": _iso(device.revoked_at),
        "active": device.revoked_at is None,
    }


def list_devices(
    session: Session, *, user_id: int | None, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Geräte eines Benutzers (``user_id``) oder aller Benutzer (``None``), neueste zuerst."""
    query = select(PushDevice).order_by(PushDevice.created_at.desc(), PushDevice.id.desc())
    if user_id is not None:
        query = query.where(PushDevice.user_id == user_id)
    if not include_inactive:
        query = query.where(PushDevice.revoked_at.is_(None))
    return [device_dict(device) for device in session.execute(query).scalars()]


# ---------------------------------------------------------------------------
# Ereignisse → Ausgangswarteschlange
# ---------------------------------------------------------------------------


def _recipient_devices(session: Session, company: Company, event: str) -> list[PushDevice]:
    """Aktive Geräte aktiver Benutzer mit Zugriff auf die Gesellschaft (Mandant oder
    global), die das Ereignis abonniert haben."""
    devices = session.execute(
        select(PushDevice)
        .join(User, User.id == PushDevice.user_id)
        .where(
            PushDevice.revoked_at.is_(None),
            User.is_active.is_(True),
            or_(User.tenant_id == company.tenant_id, User.tenant_id.is_(None)),
        )
        .order_by(PushDevice.id)
    ).scalars()
    return [device for device in devices if event in (device.events or [])]


def enqueue_company_event(
    session: Session,
    *,
    company_id: int,
    event: str,
    body: str,
    config: Mapping[str, Any] | None = None,
) -> int:
    """Legt für jedes passende Gerät eine Mitteilung an; liefert die Anzahl.

    Läuft in der Transaktion des Aufrufers (kein Commit), damit Mitteilungen nur für
    tatsächlich gespeicherte Änderungen entstehen.
    """
    if not push_enabled(config):
        return 0
    company = session.get(Company, company_id)
    if company is None:
        return 0
    devices = _recipient_devices(session, company, event)
    for device in devices:
        session.add(
            PushNotification(
                device_id=device.id,
                company_id=company.id,
                event=event,
                title=TITLE,
                body=body[:200],
            )
        )
    return len(devices)


def notify_bank_transactions(session: Session, *, company_id: int, count: int) -> int:
    if count <= 0:
        return 0
    body = "1 neuer Bankumsatz" if count == 1 else f"{count} neue Bankumsätze"
    return enqueue_company_event(
        session, company_id=company_id, event=EVENT_BANK_TRANSACTIONS, body=body
    )


def notify_elster_submission(
    session: Session, *, company_id: int, document: str, period_label: str, status: str
) -> int:
    if status == "transmitted":
        body = f"{document} {period_label} an ELSTER übermittelt"
    else:
        body = f"ELSTER-Übermittlung fehlgeschlagen: {document} {period_label}"
    return enqueue_company_event(
        session, company_id=company_id, event=EVENT_ELSTER_SUBMISSION, body=body
    )


def enqueue_overdue_digests(
    session: Session,
    *,
    today: date | None = None,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> int:
    """Tägliche Zusammenfassung überfälliger offener Posten je Gesellschaft.

    Höchstens eine Mitteilung je Gesellschaft und Tag; Gesellschaften ohne
    überfällige Posten bekommen keine.
    """
    if not push_enabled(config):
        return 0
    now = now or _utcnow()
    today = today or now.astimezone(DIGEST_TIMEZONE).date()
    day_start = datetime.combine(today, time.min, tzinfo=DIGEST_TIMEZONE).astimezone(
        timezone.utc
    )
    overdue = session.execute(
        select(OpenItem.company_id, func.count())
        .where(
            OpenItem.status == "open",
            OpenItem.due_date.is_not(None),
            OpenItem.due_date < today,
        )
        .group_by(OpenItem.company_id)
    ).all()
    already_sent = set(
        session.execute(
            select(PushNotification.company_id).where(
                PushNotification.event == EVENT_OVERDUE_OPEN_ITEMS,
                PushNotification.created_at >= day_start,
            )
        ).scalars()
    )
    created = 0
    for company_id, count in overdue:
        if company_id in already_sent:
            continue
        body = (
            "1 überfälliger offener Posten"
            if count == 1
            else f"{count} überfällige offene Posten"
        )
        created += enqueue_company_event(
            session,
            company_id=company_id,
            event=EVENT_OVERDUE_OPEN_ITEMS,
            body=body,
            config=config,
        )
    return created


def enqueue_test_notification(
    session: Session, device: PushDevice, *, config: Mapping[str, Any] | None = None
) -> PushNotification:
    """Testmitteilung an ein Gerät (unabhängig von den abonnierten Ereignissen)."""
    if not push_enabled(config):
        raise PushError("Push-Mitteilungen sind nicht konfiguriert (PUSH_RELAY_URL fehlt).")
    if device.revoked_at is not None:
        raise PushError("Das Gerät ist widerrufen.")
    notification = PushNotification(
        device_id=device.id,
        company_id=None,
        event=EVENT_TEST,
        title=TITLE,
        body="Testmitteilung: Push-Mitteilungen funktionieren.",
    )
    session.add(notification)
    session.flush()
    return notification


# ---------------------------------------------------------------------------
# Dispatcher → Push-Relay
# ---------------------------------------------------------------------------


def http_relay_transport(
    url: str, token: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8") or "{}")


def _relay_item(notification: PushNotification) -> dict[str, Any]:
    device = notification.device
    data: dict[str, Any] = {"event": notification.event}
    if notification.company_id is not None:
        data["company_id"] = notification.company_id
    if device.server_url:
        data["server_url"] = device.server_url
    return {
        "id": notification.id,
        "device_token": device.device_token,
        "environment": device.environment,
        "title": notification.title,
        "body": notification.body,
        "category": notification.event,
        "thread_id": (
            f"company-{notification.company_id}" if notification.company_id else "system"
        ),
        "data": data,
    }


def _record_failure(notification: PushNotification, error: str) -> None:
    notification.attempts += 1
    notification.last_error = error[:500]
    if notification.attempts >= MAX_ATTEMPTS:
        notification.status = "failed"


def dispatch_pending(
    session: Session,
    *,
    config: Mapping[str, Any] | None = None,
    transport: RelayTransport | None = None,
    now: datetime | None = None,
    limit: int = DISPATCH_BATCH,
) -> dict[str, int]:
    """Übergibt offene Mitteilungen an das Relay und committet das Ergebnis."""
    config = _config(config)
    counts = {"sent": 0, "failed": 0, "discarded": 0, "pending": 0}
    if not push_enabled(config):
        return counts
    now = now or _utcnow()
    transport = transport or http_relay_transport

    stale = session.execute(
        PushNotification.__table__.update()
        .where(
            PushNotification.status == "pending",
            PushNotification.created_at < now - STALE_AFTER,
        )
        .values(status="discarded", last_error="Veraltet, nicht mehr zugestellt.")
    )
    counts["discarded"] += stale.rowcount or 0

    pending = (
        session.execute(
            select(PushNotification)
            .where(PushNotification.status == "pending")
            .order_by(PushNotification.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    deliverable = []
    for notification in pending:
        if notification.device.revoked_at is not None:
            notification.status = "discarded"
            notification.last_error = "Gerät widerrufen."
            counts["discarded"] += 1
        else:
            deliverable.append(notification)

    if deliverable:
        url = f"{str(config['PUSH_RELAY_URL']).rstrip('/')}/v1/notifications"
        token = str(config.get("PUSH_RELAY_TOKEN") or "")
        payload = {"notifications": [_relay_item(item) for item in deliverable]}
        try:
            response = transport(url, token, payload, RELAY_TIMEOUT_SECONDS)
            results = {
                result.get("id"): result
                for result in (response.get("results") or [])
                if isinstance(result, dict)
            }
        except Exception as exc:  # Netzwerk-, HTTP- und JSON-Fehler gleich behandeln
            logger.warning("Push-Relay nicht erreichbar: %s", type(exc).__name__)
            for notification in deliverable:
                _record_failure(notification, f"Relay nicht erreichbar ({type(exc).__name__}).")
        else:
            for notification in deliverable:
                result = results.get(notification.id) or {}
                status = result.get("status")
                if status == "sent":
                    notification.status = "sent"
                    notification.sent_at = now
                    notification.attempts += 1
                elif status == "invalid_token":
                    notification.status = "failed"
                    notification.attempts += 1
                    notification.last_error = "Gerätetoken ungültig (abgemeldet)."
                    revoke_device(
                        session,
                        notification.device,
                        actor="push-relay",
                        reason="invalid_token",
                    )
                else:
                    _record_failure(
                        notification,
                        str(result.get("error") or "Keine Rückmeldung des Relays."),
                    )
        for notification in deliverable:
            if notification.status in counts:
                counts[notification.status] += 1

    session.execute(
        PushNotification.__table__.delete().where(
            PushNotification.status != "pending",
            PushNotification.created_at < now - RETENTION,
        )
    )
    session.commit()
    return counts


def digest_due(now: datetime, config: Mapping[str, Any] | None = None) -> bool:
    """Ob die tägliche Zusammenfassung fällig ist (ab ``PUSH_DIGEST_HOUR`` Ortszeit)."""
    hour = int(_config(config).get("PUSH_DIGEST_HOUR") or DEFAULT_DIGEST_HOUR)
    return now.astimezone(DIGEST_TIMEZONE).hour >= hour


def run_dispatch_cycle(
    session: Session,
    *,
    config: Mapping[str, Any] | None = None,
    transport: RelayTransport | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Ein Durchlauf des Dispatchers: fällige Zusammenfassungen anlegen, dann zustellen."""
    now = now or _utcnow()
    digests = 0
    if push_enabled(config) and digest_due(now, config):
        digests = enqueue_overdue_digests(session, now=now, config=config)
        session.commit()
    counts = dispatch_pending(session, config=config, transport=transport, now=now)
    counts["digests"] = digests
    return counts
