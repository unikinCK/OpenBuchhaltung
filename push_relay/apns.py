"""Zustellung an den Apple Push Notification service (APNs) per HTTP/2 und Token-Auth.

Apple verlangt HTTP/2; ``httpx`` mit ``h2`` hält eine Verbindung je Umgebung offen.
Authentifiziert wird mit einem ES256-JWT aus dem APNs-Schlüssel (``.p8``), Key-ID und
Team-ID; Apple akzeptiert ein Token 20–60 Minuten, es wird nach 50 Minuten erneuert.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

logger = logging.getLogger("push_relay.apns")

APNS_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
TOKEN_LIFETIME_SECONDS = 50 * 60
EXPIRATION_SECONDS = 24 * 60 * 60
MAX_PAYLOAD_BYTES = 4096
REQUEST_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Notification:
    """Eine geprüfte Mitteilung aus dem Relay-Protokoll v1."""

    id: int
    device_token: str
    environment: str
    title: str
    body: str
    category: str
    thread_id: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    status: str  # sent | invalid_token | error
    error: str | None = None


class Sender(Protocol):
    mode: str

    def send(self, notification: Notification) -> DeliveryResult: ...


def apns_payload(notification: Notification) -> bytes:
    """APNs-JSON: Alert mit Titel/Text, Thread und Kategorie; ``mutable-content``
    erlaubt der App, die Mitteilung lokal anzureichern (Notification Service Extension)."""
    payload = {
        "aps": {
            "alert": {"title": notification.title, "body": notification.body},
            "sound": "default",
            "thread-id": notification.thread_id,
            "category": notification.category,
            "mutable-content": 1,
        },
        "data": notification.data,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def provider_token(private_key: ec.EllipticCurvePrivateKey, *, key_id: str, team_id: str,
                   issued_at: int) -> str:
    """ES256-JWT für APNs (Header ``alg``/``kid``, Claims ``iss``/``iat``)."""
    header = _b64url(json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iss": team_id, "iat": issued_at}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    r, s = decode_dss_signature(private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"{header}.{claims}.{_b64url(signature)}"


def load_private_key(path: str) -> ec.EllipticCurvePrivateKey:
    with open(path, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("Der APNs-Schlüssel muss ein EC-Schlüssel (P-256, .p8) sein.")
    return key


class DryRunSender:
    """Trockenlauf ohne Apple-Account: meldet jede Mitteilung als zugestellt."""

    mode = "dry-run"

    def send(self, notification: Notification) -> DeliveryResult:
        logger.info(
            "dry-run id=%s env=%s category=%s token=…%s",
            notification.id,
            notification.environment,
            notification.category,
            notification.device_token[-6:],
        )
        return DeliveryResult("sent")


class ApnsSender:
    """Echte Zustellung an APNs. ``clients`` ersetzt in Tests die HTTP/2-Clients."""

    mode = "apns"

    def __init__(
        self,
        *,
        private_key: ec.EllipticCurvePrivateKey,
        key_id: str,
        team_id: str,
        topic: str,
        clients: dict[str, Any] | None = None,
        clock=time.time,
    ) -> None:
        self._private_key = private_key
        self._key_id = key_id
        self._team_id = team_id
        self._topic = topic
        self._clients = clients or {}
        self._clock = clock
        self._token: str | None = None
        self._token_issued_at = 0
        self._lock = threading.Lock()

    def _client(self, environment: str):
        with self._lock:
            client = self._clients.get(environment)
            if client is None:
                import httpx  # erst hier: nur der Relay-Betrieb braucht httpx/h2

                client = httpx.Client(
                    http2=True,
                    base_url=APNS_HOSTS[environment],
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                self._clients[environment] = client
            return client

    def _bearer(self, *, refresh: bool = False) -> str:
        with self._lock:
            now = int(self._clock())
            expired = now - self._token_issued_at >= TOKEN_LIFETIME_SECONDS
            if refresh or self._token is None or expired:
                self._token = provider_token(
                    self._private_key, key_id=self._key_id, team_id=self._team_id, issued_at=now
                )
                self._token_issued_at = now
            return self._token

    def send(self, notification: Notification) -> DeliveryResult:
        body = apns_payload(notification)
        if len(body) > MAX_PAYLOAD_BYTES:
            return DeliveryResult("error", "PayloadTooLarge")
        for attempt in (1, 2):
            headers = {
                "authorization": f"bearer {self._bearer(refresh=attempt == 2)}",
                "apns-topic": self._topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
                "apns-expiration": str(int(self._clock()) + EXPIRATION_SECONDS),
            }
            try:
                response = self._client(notification.environment).post(
                    f"/3/device/{notification.device_token}", content=body, headers=headers
                )
            except Exception as exc:  # Netzwerkfehler: Instanz versucht es erneut
                return DeliveryResult("error", f"APNs nicht erreichbar ({type(exc).__name__})")
            if response.status_code == 200:
                return DeliveryResult("sent")
            reason = _reason(response)
            if response.status_code == 410:
                return DeliveryResult("invalid_token", reason or "Unregistered")
            if response.status_code == 403 and reason == "ExpiredProviderToken" and attempt == 1:
                continue
            return DeliveryResult("error", reason or f"HTTP {response.status_code}")
        return DeliveryResult("error", "ExpiredProviderToken")


def _reason(response) -> str | None:
    try:
        return response.json().get("reason")
    except Exception:  # leere oder kaputte Antwort
        return None
