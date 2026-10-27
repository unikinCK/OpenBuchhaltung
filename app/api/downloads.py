"""Download-Links: Dateien der API ohne Anmeldung über einen signierten Link abrufen.

Datei-Endpunkte (Belege, CSV-Exporte, Prüferpaket, ELSTER-Nutzlast) tragen den
Dekorator :func:`linkable_download`. Mit ``?download_link=1`` liefern sie statt der
Datei einen Link ``/api/v1/downloads/<token>`` (siehe
:mod:`app.services.download_links`), den KI-Chats und MCP-Clients dem Benutzer
anzeigen. Nur so markierte Endpunkte lassen sich über einen Link abrufen.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Response, current_app, jsonify, request, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.http import parse_options_header

from app.api.blueprint import api_bp
from app.api.helpers import bool_arg
from app.auth import active_api_user, api_has_global_access, current_api_user
from app.services.download_links import (
    DOWNLOAD_LINK_ARG,
    DownloadGrant,
    DownloadLinkError,
    download_link_ttl_seconds,
    issue_download_token,
    read_download_token,
)
from app.services.internal_api import InProcessApiClient

_LINKABLE_ATTR = "openbuchhaltung_linkable_download"
_PASSTHROUGH_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Disposition",
    "X-OpenBuchhaltung-SHA256",
)


def download_link_payload(path: str, params: dict[str, str] | None = None) -> dict[str, object]:
    """Signierter Link auf ``GET /api/v1{path}`` für den aktuellen API-Benutzer."""
    user = current_api_user()
    grant = DownloadGrant(
        path=path,
        params=dict(params or {}),
        user_id=user.get("id") if user else None,
        global_access=api_has_global_access(),
    )
    token = issue_download_token(current_app.config["SECRET_KEY"], grant)
    ttl = download_link_ttl_seconds(current_app.config)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
    return {
        "download_url": url_for("api.download_via_link", token=token, _external=True),
        "download_path": url_for("api.download_via_link", token=token),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "expires_in_seconds": ttl,
    }


def linkable_download(view):
    """Datei-Endpunkt, der mit ``?download_link=1`` einen Download-Link liefert.

    Der Endpunkt läuft dafür einmal vollständig (Fehler wie ein fehlendes
    Wirtschaftsjahr kommen sofort zurück, nicht erst beim Klick auf den Link);
    die Datei selbst wird verworfen.
    """

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not bool_arg(DOWNLOAD_LINK_ARG):
            return view(*args, **kwargs)
        response = current_app.make_response(view(*args, **kwargs))
        if response.status_code != 200:
            return response
        try:
            _, options = parse_options_header(response.headers.get("Content-Disposition", ""))
            params = {key: value for key, value in request.args.items() if key != DOWNLOAD_LINK_ARG}
            path = request.path.removeprefix(api_bp.url_prefix or "")
            return jsonify(
                {
                    "file_name": options.get("filename"),
                    "mime_type": response.mimetype,
                    "file_size_bytes": response.content_length,
                    **download_link_payload(path, params),
                }
            )
        finally:
            response.close()

    setattr(wrapped, _LINKABLE_ATTR, True)
    return wrapped


def _linkable_path(path: str) -> bool:
    adapter = current_app.url_map.bind("localhost")
    try:
        endpoint, _ = adapter.match(f"{api_bp.url_prefix}{path}", method="GET")
    except HTTPException:
        return False
    return bool(getattr(current_app.view_functions.get(endpoint), _LINKABLE_ATTR, False))


def _link_error(message: str, status: int):
    response = jsonify({"error": message})
    response.headers["Cache-Control"] = "no-store"
    return response, status


@api_bp.get("/downloads/<token>")
def download_via_link(token: str):
    """Liefert die Datei eines Download-Links (ohne Anmeldung, der Token ist die Berechtigung)."""
    try:
        grant = read_download_token(
            current_app.config["SECRET_KEY"],
            token,
            max_age=download_link_ttl_seconds(current_app.config),
        )
    except DownloadLinkError as exc:
        if exc.code == "expired":
            return _link_error("Download link expired. Request a new link.", 410)
        return _link_error(str(exc), 404)
    if not _linkable_path(grant.path):
        return _link_error("Download link is invalid.", 404)

    api_user = None
    global_access = grant.global_access
    if grant.user_id is not None:
        api_user = active_api_user(grant.user_id)
        if api_user is None:
            return _link_error("Download link is no longer valid.", 403)
        global_access = api_user["tenant_id"] is None

    client = InProcessApiClient(
        current_app._get_current_object(), api_user=api_user, global_access=global_access
    )
    inner = client.open_raw("GET", grant.path, params=grant.params, buffered=False)
    if inner.status_code != 200:
        body = inner.get_data()
        inner.close()
        response = Response(body, status=inner.status_code, content_type=inner.content_type)
        response.headers["Cache-Control"] = "no-store"
        return response

    response = Response(inner.response, status=200, direct_passthrough=True)
    for name in _PASSTHROUGH_HEADERS:
        if name in inner.headers:
            response.headers[name] = inner.headers[name]
    response.headers.setdefault("Content-Disposition", "attachment")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.call_on_close(inner.close)
    return response
