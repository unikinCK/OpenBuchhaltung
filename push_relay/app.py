"""Push-Relay für OpenBuchhaltung: nimmt Mitteilungen von Instanzen an und stellt sie über
APNs zu (Protokoll v1, siehe README „Push-Relay“).

Das Relay speichert nichts: Inhalte und Gerätetokens gehen nur durch, das Log enthält
je Anfrage Instanz, Anzahl und Ergebnis. Instanzen weisen sich mit einem eigenen
Schlüssel aus, von dem das Relay nur den SHA-256-Hash kennt.

Konfiguration (Umgebung):

* ``RELAY_INSTANCE_KEYS`` – ``name:sha256hex`` je Instanz, kommagetrennt
  (erzeugen mit ``python -m push_relay new-instance-key <name>``).
* ``RELAY_APNS_MODE`` – ``apns`` (Standard) oder ``dry-run`` (ohne Apple-Account).
* ``RELAY_APNS_KEY_FILE``, ``RELAY_APNS_KEY_ID``, ``RELAY_APNS_TEAM_ID``,
  ``RELAY_APNS_TOPIC`` (Bundle-ID, Standard ``de.openbuchhaltung.app``).
* ``RELAY_RATE_LIMIT_PER_MINUTE`` – Mitteilungen je Instanz und Minute (Standard 600).

Betrieb mit genau einem gunicorn-Worker (das Rate-Limit liegt im Prozess).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Mapping
from typing import Any

from flask import Flask, jsonify, request

from push_relay.apns import ApnsSender, DryRunSender, Notification, Sender, load_private_key

logger = logging.getLogger("push_relay")

PROTOCOL_PATH = "/v1/notifications"
MAX_BATCH = 100
MAX_DATA_BYTES = 1024
DEFAULT_TOPIC = "de.openbuchhaltung.app"
DEFAULT_RATE_LIMIT_PER_MINUTE = 600
ENVIRONMENTS = ("production", "sandbox")
_TOKEN_RE = re.compile(r"^[0-9a-f]{64,200}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ConfigError(ValueError):
    """Unvollständige oder ungültige Relay-Konfiguration."""


def hash_instance_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_instance_keys(raw: str) -> dict[str, str]:
    """``name:sha256hex,…`` → {hash: name}."""
    keys: dict[str, str] = {}
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        name, _, digest = entry.partition(":")
        name, digest = name.strip(), digest.strip().lower()
        if not name or not _HASH_RE.match(digest):
            raise ConfigError(f"Ungültiger Eintrag in RELAY_INSTANCE_KEYS: {name or entry[:20]}")
        keys[digest] = name
    return keys


class RateLimiter:
    """Gleitendes Fenster je Instanz: höchstens ``limit`` Mitteilungen pro Minute."""

    def __init__(self, limit: int, clock=time.monotonic) -> None:
        self._limit = limit
        self._clock = clock
        self._sent: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, instance: str) -> bool:
        with self._lock:
            now = self._clock()
            window = self._sent.setdefault(instance, deque())
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self._limit:
                return False
            window.append(now)
            return True


def _sender_from_config(config: Mapping[str, Any]) -> Sender:
    mode = str(config.get("RELAY_APNS_MODE") or "apns")
    if mode == "dry-run":
        return DryRunSender()
    if mode != "apns":
        raise ConfigError("RELAY_APNS_MODE muss apns oder dry-run sein.")
    missing = [
        name
        for name in ("RELAY_APNS_KEY_FILE", "RELAY_APNS_KEY_ID", "RELAY_APNS_TEAM_ID")
        if not config.get(name)
    ]
    if missing:
        raise ConfigError(f"Für RELAY_APNS_MODE=apns fehlen: {', '.join(missing)}.")
    return ApnsSender(
        private_key=load_private_key(str(config["RELAY_APNS_KEY_FILE"])),
        key_id=str(config["RELAY_APNS_KEY_ID"]),
        team_id=str(config["RELAY_APNS_TEAM_ID"]),
        topic=str(config.get("RELAY_APNS_TOPIC") or DEFAULT_TOPIC),
    )


def _parse_notification(item: Any) -> Notification | str:
    """Prüft einen Eintrag des Protokolls; liefert die Mitteilung oder einen Fehlertext."""
    if not isinstance(item, dict):
        return "Eintrag muss ein Objekt sein."
    token = str(item.get("device_token") or "").lower()
    if not _TOKEN_RE.match(token):
        return "device_token ungültig."
    environment = item.get("environment")
    if environment not in ENVIRONMENTS:
        return "environment ungültig."
    title, body = item.get("title"), item.get("body")
    if not isinstance(title, str) or not isinstance(body, str) or not title or not body:
        return "title und body sind Pflicht."
    if len(title) > 100 or len(body) > 200:
        return "title oder body zu lang."
    category = str(item.get("category") or "")[:40]
    thread_id = str(item.get("thread_id") or "")[:64]
    data = item.get("data") or {}
    if not isinstance(data, dict) or len(json.dumps(data)) > MAX_DATA_BYTES:
        return "data ungültig oder zu groß."
    return Notification(
        id=item["id"],
        device_token=token,
        environment=environment,
        title=title,
        body=body,
        category=category,
        thread_id=thread_id,
        data=data,
    )


def create_app(config: Mapping[str, Any] | None = None, *, sender: Sender | None = None) -> Flask:
    app = Flask(__name__)
    settings = dict(os.environ)
    settings.update(config or {})
    instance_keys = parse_instance_keys(str(settings.get("RELAY_INSTANCE_KEYS") or ""))
    if not instance_keys:
        raise ConfigError("RELAY_INSTANCE_KEYS ist leer – keine Instanz dürfte senden.")
    sender = sender or _sender_from_config(settings)
    limiter = RateLimiter(
        int(settings.get("RELAY_RATE_LIMIT_PER_MINUTE") or DEFAULT_RATE_LIMIT_PER_MINUTE)
    )

    def _instance() -> str | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        digest = hash_instance_key(header.removeprefix("Bearer ").strip())
        for known, name in instance_keys.items():
            if hmac.compare_digest(known, digest):
                return name
        return None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "mode": sender.mode, "instances": len(instance_keys)})

    @app.post(PROTOCOL_PATH)
    def notifications():
        instance = _instance()
        if instance is None:
            return jsonify({"error": "unauthorized"}), 401
        payload = request.get_json(silent=True)
        items = payload.get("notifications") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) > MAX_BATCH:
            return jsonify({"error": f"notifications: Liste mit höchstens {MAX_BATCH}"}), 400
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), int) for item in items):
            return jsonify({"error": "Jeder Eintrag braucht eine ganzzahlige id."}), 400

        results = []
        for item in items:
            parsed = _parse_notification(item)
            if isinstance(parsed, str):
                results.append({"id": item["id"], "status": "error", "error": parsed})
                continue
            if not limiter.allow(instance):
                results.append({"id": parsed.id, "status": "error", "error": "rate_limited"})
                continue
            delivery = sender.send(parsed)
            result = {"id": parsed.id, "status": delivery.status}
            if delivery.error:
                result["error"] = delivery.error
            results.append(result)

        summary = Counter(result["status"] for result in results)
        logger.info(
            "instance=%s count=%d %s",
            instance,
            len(results),
            " ".join(f"{status}={count}" for status, count in sorted(summary.items())),
        )
        return jsonify({"results": results}), 200

    return app
