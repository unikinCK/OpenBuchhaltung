"""Signierte, zeitlich begrenzte Download-Links auf Dateien der REST-API.

Belege und Exporte passen nicht als Base64 durch einen KI-Chat (MCP-Connector,
In-App-Chat). Stattdessen stellt die API einen Link ``/api/v1/downloads/<token>``
aus, den der Benutzer im Browser ohne Anmeldung öffnet. Der Token ist mit dem
``SECRET_KEY`` signiert und enthält nur den API-Pfad, dessen Query-Parameter und
den Auth-Kontext des Ausstellers. Beim Abruf läuft der Pfad mit genau diesem
Kontext erneut durch die REST-API: Mandanten-Scope und Rolle werden also noch
einmal geprüft, und ein inzwischen gesperrter Benutzer bekommt nichts mehr.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

DOWNLOAD_LINK_SALT = "openbuchhaltung-download-link"
DEFAULT_DOWNLOAD_LINK_TTL_SECONDS = 900
# Query-Parameter, mit dem ein Datei-Endpunkt statt der Datei einen Link liefert.
DOWNLOAD_LINK_ARG = "download_link"
# Öffentlicher Pfad der Downloads; der MCP-HTTP-Server reicht ihn an die App durch.
DOWNLOAD_ROUTE_PREFIX = "/api/v1/downloads/"


class DownloadLinkError(ValueError):
    """Token ungültig (``invalid``) oder abgelaufen (``expired``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DownloadGrant:
    """Was ein Download-Link erlaubt: ein GET auf ``path`` mit ``params``."""

    path: str  # API-Pfad ohne /api/v1, z. B. /documents/12/download
    params: dict[str, str] = field(default_factory=dict)
    user_id: int | None = None
    global_access: bool = False


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key,
        salt=DOWNLOAD_LINK_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def download_link_ttl_seconds(config: Any) -> int:
    raw = config.get("DOWNLOAD_LINK_TTL_SECONDS")
    try:
        ttl = int(raw) if raw not in (None, "") else DEFAULT_DOWNLOAD_LINK_TTL_SECONDS
    except (TypeError, ValueError):
        ttl = DEFAULT_DOWNLOAD_LINK_TTL_SECONDS
    return max(ttl, 1)


def issue_download_token(secret_key: str, grant: DownloadGrant) -> str:
    payload: dict[str, Any] = {"p": grant.path}
    if grant.params:
        payload["q"] = dict(grant.params)
    if grant.user_id is not None:
        payload["u"] = grant.user_id
    if grant.global_access:
        payload["g"] = 1
    return _serializer(secret_key).dumps(payload)


def read_download_token(secret_key: str, token: str, *, max_age: int) -> DownloadGrant:
    try:
        payload = _serializer(secret_key).loads(token, max_age=max_age)
    except SignatureExpired as exc:
        raise DownloadLinkError("expired", "Download link expired.") from exc
    except BadSignature as exc:
        raise DownloadLinkError("invalid", "Download link is invalid.") from exc

    path = payload.get("p") if isinstance(payload, dict) else None
    params = payload.get("q", {}) if isinstance(payload, dict) else None
    user_id = payload.get("u") if isinstance(payload, dict) else None
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(params, dict)
        or not (user_id is None or isinstance(user_id, int))
    ):
        raise DownloadLinkError("invalid", "Download link is invalid.")
    return DownloadGrant(
        path=path,
        params={str(key): str(value) for key, value in params.items()},
        user_id=user_id,
        global_access=bool(payload.get("g")),
    )
