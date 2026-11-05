"""Zwei-Faktor-Anmeldung: TOTP, Login-Schritt, Einrichtung, Rollenpflicht, Admin-Reset."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select

from app import create_app
from app.auth import TWO_FACTOR_PENDING_KEY, hash_api_token, hash_password
from app.db import _alembic_config
from app.services.audit_export import EXCLUDED_EXPORT_FIELDS, build_audit_export_package
from app.services.chat import chat_tool_blocked
from app.services.mcp_server import TOOLS
from app.services.two_factor import (
    TwoFactorError,
    confirm_setup,
    decrypt_secret,
    hotp,
    matching_step,
    provisioning_uri,
    qr_code_svg,
    start_setup,
    totp_code,
    totp_step,
    verify_code,
)
from domain.models import AuditLog, Company, LoginAttempt, Tenant, TwoFactorRecoveryCode, User

PASSWORD = "geheim123"
LOGIN_FORM = "/auth/login"
SECOND_FACTOR = "/auth/login/zweiter-faktor"
SETUP_PAGE = "/auth/zwei-faktor"
# Die Uhr der Codes steht: Sonst fiele ein Code bei einem langsamen Lauf aus dem Fenster von
# einem Zeitschritt (Passwort-Hashes, parallele Prozesse).
TOTP_NOW = time.time()


@pytest.fixture(autouse=True)
def _frozen_totp_clock(monkeypatch):
    monkeypatch.setattr("app.services.two_factor.time", SimpleNamespace(time=lambda: TOTP_NOW))


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'two_factor.db'}",
        "DOCUMENT_UPLOAD_DIR": str(tmp_path / "uploads"),
    }
    config.update(overrides)
    return create_app(config)


def _seed_user(
    app,
    username: str = "maria",
    *,
    role: str = "Buchhalter",
    tenant_name: str | None = "Mandant A",
    token: str | None = None,
) -> int:
    with app.extensions["db_session_factory"]() as session:
        tenant_id = None
        if tenant_name:
            tenant = session.execute(
                select(Tenant).where(Tenant.name == tenant_name)
            ).scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(name=tenant_name)
                session.add(tenant)
                session.flush()
            tenant_id = tenant.id
        user = User(
            username=username,
            password_hash=hash_password(PASSWORD),
            role=role,
            tenant_id=tenant_id,
            api_token_hash=hash_api_token(token) if token else None,
            api_token_last4=token[-4:] if token else None,
        )
        session.add(user)
        session.commit()
        return user.id


def _enable(app, user_id: int) -> tuple[str, list[str]]:
    """Aktiviert 2FA; der verbrauchte Zeitschritt liegt in der Vergangenheit."""
    with app.app_context(), app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        secret = start_setup(session, user, password=PASSWORD, actor="test")
        past = TOTP_NOW - 300
        codes = confirm_setup(session, user, code=totp_code(secret, past), actor="test", at=past)
        session.commit()
        return secret, codes


def _now_code(secret: str) -> str:
    return totp_code(secret, TOTP_NOW)


def _login(client, username: str = "maria", password: str = PASSWORD, **kwargs):
    return client.post(LOGIN_FORM, data={"username": username, "password": password}, **kwargs)


def _second_factor(client, code: str, **kwargs):
    return client.post(SECOND_FACTOR, data={"code": code}, **kwargs)


def _session_keys(client) -> set[str]:
    with client.session_transaction() as flask_session:
        return set(flask_session.keys())


def _audit_actions(app) -> list[str]:
    with app.extensions["db_session_factory"]() as session:
        rows = session.execute(select(AuditLog).order_by(AuditLog.sequence_number)).scalars().all()
        return [row.action for row in rows]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_state(app, user_id: int) -> User:
    with app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        session.expunge(user)
        return user


# ---------------------------------------------------------------------------
# TOTP-Kern
# ---------------------------------------------------------------------------

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii")


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_matches_rfc_6238_sha1_vectors(timestamp, expected):
    assert totp_code(RFC_SECRET, timestamp, digits=8) == expected
    assert totp_code(RFC_SECRET, timestamp) == expected[-6:]


def test_hotp_matches_rfc_4226_vectors():
    secret = b"12345678901234567890"
    expected = ["755224", "287082", "359152", "969429", "338314"]
    assert [hotp(secret, counter) for counter in range(5)] == expected


def test_time_window_allows_exactly_one_step_of_drift():
    at = 1_700_000_010
    code = totp_code(RFC_SECRET, at)
    step = totp_step(at)
    assert matching_step(RFC_SECRET, code, at=at) == step
    assert matching_step(RFC_SECRET, code, at=at - 30) == step
    assert matching_step(RFC_SECRET, code, at=at + 30) == step
    assert matching_step(RFC_SECRET, code, at=at - 60) is None
    assert matching_step(RFC_SECRET, code, at=at + 60) is None


def test_same_time_step_cannot_be_used_twice_and_no_older_step(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    secret, _ = _enable(app, user_id)
    at = TOTP_NOW
    with app.app_context(), app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        assert verify_code(session, user, totp_code(secret, at), at=at) == "totp"
        assert verify_code(session, user, totp_code(secret, at), at=at) is None
        # Auch der vorige Schritt ist im Fenster, aber älter als der verbrauchte.
        assert verify_code(session, user, totp_code(secret, at - 30), at=at) is None
        assert verify_code(session, user, totp_code(secret, at + 30), at=at) == "totp"


@pytest.mark.parametrize(
    "code", ["", "abc", "12345", "1234567", "١٢٣٤٥٦", "000000-", "x" * 16, None, "' OR 1=1 --"]
)
def test_malformed_codes_are_rejected(tmp_path, code):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    _enable(app, user_id)
    with app.app_context(), app.extensions["db_session_factory"]() as session:
        assert verify_code(session, session.get(User, user_id), code) is None


def test_secret_is_encrypted_at_rest_and_unreadable_after_key_rotation(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    secret, codes = _enable(app, user_id)
    stored = _user_state(app, user_id).totp_secret_encrypted
    assert secret not in stored
    with app.app_context():
        assert decrypt_secret(stored) == secret
    assert decrypt_secret(stored, secret_key="ein-anderer-schluessel") is None

    # Neuer SECRET_KEY: TOTP schlägt fehl, Wiederherstellungscode und Admin-Reset bleiben.
    rotated = _create_app(tmp_path, SECRET_KEY="ein-anderer-schluessel")
    with rotated.app_context(), rotated.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        assert verify_code(session, user, _now_code(secret)) is None
    client = rotated.test_client()
    _login(client)
    assert _second_factor(client, _now_code(secret)).headers["Location"].endswith(SECOND_FACTOR)
    assert _second_factor(client, codes[0]).headers["Location"] == "/"
    status = client.get("/api/v1/users/me/two-factor").get_json()
    assert status["enabled"] is True and status["secret_readable"] is False
    assert "nicht mehr lesbar" in client.get(SETUP_PAGE).get_data(as_text=True)


def test_provisioning_uri_and_qr_code():
    uri = provisioning_uri("ABC234", username="maria müller", host="obh.test")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    assert parts.path == "/OpenBuchhaltung:" + quote("maria müller", safe="") + "@obh.test"
    query = parse_qs(parts.query)
    assert query == {
        "secret": ["ABC234"],
        "issuer": ["OpenBuchhaltung"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }
    svg = qr_code_svg(uri)
    assert svg.startswith("<svg") and "<script" not in svg and "ABC234" not in svg


# ---------------------------------------------------------------------------
# Login mit zweitem Faktor
# ---------------------------------------------------------------------------


def test_password_alone_only_creates_intermediate_state(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    user_id = _seed_user(app)
    secret, _ = _enable(app, user_id)
    client = app.test_client()

    response = _login(client)
    assert response.status_code == 302
    assert response.headers["Location"] == SECOND_FACTOR
    with client.session_transaction() as flask_session:
        assert "user" not in flask_session
        pending = flask_session[TWO_FACTOR_PENDING_KEY]
        assert pending["user_id"] == user_id
        assert pending["expires_at"] - time.time() <= 300

    # Weder UI noch API noch Passwortseite sind schon erreichbar.
    for path in ("/", "/verwaltung", "/auth/password", "/auth/apps", SETUP_PAGE):
        blocked = client.get(path)
        assert blocked.status_code == 302, path
        assert LOGIN_FORM in blocked.headers["Location"], path
    assert client.get("/api/v1/users/me").status_code == 401
    assert client.get("/api/v1/companies").status_code == 401
    assert "Zweiter Faktor" in client.get(SECOND_FACTOR).get_data(as_text=True)

    final = _second_factor(client, _now_code(secret))
    assert final.status_code == 302 and final.headers["Location"] == "/"
    with client.session_transaction() as flask_session:
        assert flask_session["user"]["id"] == user_id
        assert TWO_FACTOR_PENDING_KEY not in flask_session
        assert flask_session.permanent is True
    assert client.get("/").status_code == 200


def test_wrong_password_gives_no_hint_about_two_factor(tmp_path):
    app = _create_app(tmp_path)
    _enable(app, _seed_user(app, "mit2fa"))
    _seed_user(app, "ohne2fa", tenant_name="Mandant B")
    responses = {}
    for name in ("mit2fa", "ohne2fa", "unbekannt"):
        client = app.test_client()
        response = _login(client, name, "falsch")
        responses[name] = (response.status_code, response.headers["Location"])
        assert TWO_FACTOR_PENDING_KEY not in _session_keys(client)
    assert len(set(responses.values())) == 1

    # Die Login-Seite selbst sagt ebenfalls nichts dazu.
    assert "Faktor" not in app.test_client().get(LOGIN_FORM).get_data(as_text=True)


def test_user_without_two_factor_logs_in_as_before(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client = app.test_client()
    assert _login(client).headers["Location"] == "/"
    with client.session_transaction() as flask_session:
        assert flask_session["user"]["id"] == user_id
    assert _audit_actions(app) == ["login"]


def test_login_as_other_user_drops_previous_session(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, "ohne2fa")
    _enable(app, _seed_user(app, "mit2fa"))
    client = app.test_client()
    _login(client, "ohne2fa")
    assert client.get("/").status_code == 200
    assert _login(client, "mit2fa").headers["Location"] == SECOND_FACTOR
    assert "user" not in _session_keys(client)
    assert client.get("/").status_code == 302


def test_replayed_code_is_rejected_at_login(tmp_path):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    code = _now_code(secret)

    first = app.test_client()
    _login(first)
    assert _second_factor(first, code).headers["Location"] == "/"

    second = app.test_client()
    _login(second)
    rejected = _second_factor(second, code)
    assert rejected.headers["Location"] == SECOND_FACTOR
    assert "user" not in _session_keys(second)
    assert _audit_actions(app)[-1] == "two_factor_failed"

    # Der nächste Zeitschritt funktioniert.
    assert _second_factor(second, totp_code(secret, TOTP_NOW + 30)).headers["Location"] == "/"


def test_failed_codes_count_in_rate_limit_and_password_does_not_reset_them(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=3)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()

    _login(client)
    assert _second_factor(client, "000000").status_code == 302
    assert _second_factor(client, "000001").status_code == 302

    # Erneutes richtiges Passwort darf den Zähler nicht zurücksetzen.
    assert _login(client).headers["Location"] == SECOND_FACTOR
    assert _second_factor(client, "000002").status_code == 302

    blocked = _second_factor(client, _now_code(secret))
    assert blocked.status_code == 429
    assert "user" not in _session_keys(client)
    # Auch der Passwortschritt ist für diese Adresse gesperrt.
    assert _login(client).status_code == 429


def test_rate_limit_for_codes_also_applies_across_addresses(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2)
    secret, _ = _enable(app, _seed_user(app))
    for address in ("198.51.100.1", "198.51.100.2"):
        client = app.test_client()
        environ = {"REMOTE_ADDR": address}
        assert _login(client, environ_base=environ).status_code == 302
        assert _second_factor(client, "000000", environ_base=environ).status_code == 302

    fresh = app.test_client()
    environ = {"REMOTE_ADDR": "198.51.100.3"}
    assert _login(fresh, environ_base=environ).headers["Location"] == SECOND_FACTOR
    assert _second_factor(fresh, _now_code(secret), environ_base=environ).status_code == 429

    # Die Admin-Entsperrung hebt auch diese Sperre auf.
    admin = app.test_client()
    _seed_user(app, "chefin", role="Admin", tenant_name=None)
    _login(admin, "chefin", environ_base={"REMOTE_ADDR": "203.0.113.9"})
    user_id = _user_id(app, "maria")
    assert admin.post(f"/users/{user_id}/unlock").status_code == 302
    assert (
        _second_factor(fresh, _now_code(secret), environ_base=environ).headers["Location"] == "/"
    )


def _user_id(app, username: str) -> int:
    with app.extensions["db_session_factory"]() as session:
        return session.execute(select(User.id).where(User.username == username)).scalar_one()


def test_successful_second_factor_clears_failed_attempts(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=3)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    _login(client)
    _second_factor(client, "000000")
    assert _second_factor(client, _now_code(secret)).headers["Location"] == "/"
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(LoginAttempt)).scalars().all() == []


def test_intermediate_state_expires(tmp_path):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    client.post(LOGIN_FORM + "?next=/buchungen", data={"username": "maria", "password": PASSWORD})
    with client.session_transaction() as flask_session:
        pending = dict(flask_session[TWO_FACTOR_PENDING_KEY])
        pending["expires_at"] = int(time.time()) - 1
        flask_session[TWO_FACTOR_PENDING_KEY] = pending

    response = _second_factor(client, _now_code(secret))
    assert response.status_code == 302
    location = urlsplit(response.headers["Location"])
    assert location.path == LOGIN_FORM
    assert parse_qs(location.query)["next"] == ["/buchungen"]
    assert "user" not in _session_keys(client)
    assert TWO_FACTOR_PENDING_KEY not in _session_keys(client)

    # Ohne Zwischenstand führt die Seite direkt zurück zum Passwort.
    assert LOGIN_FORM in client.get(SECOND_FACTOR).headers["Location"]


def test_second_step_rechecks_user_state(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    secret, _ = _enable(app, user_id)
    client = app.test_client()
    _login(client)
    with app.extensions["db_session_factory"]() as session:
        session.get(User, user_id).is_active = False
        session.commit()
    assert LOGIN_FORM in _second_factor(client, _now_code(secret)).headers["Location"]
    assert "user" not in _session_keys(client)


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/phish",
        "//evil.example/phish",
        "/\\evil.example",
        "javascript:alert(1)",
    ],
)
def test_next_cannot_redirect_to_foreign_host(tmp_path, target):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    client.post(
        LOGIN_FORM,
        query_string={"next": target},
        data={"username": "maria", "password": PASSWORD},
    )
    assert _second_factor(client, _now_code(secret)).headers["Location"] == "/"


def test_next_inside_session_is_validated_again(tmp_path):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    _login(client)
    with client.session_transaction() as flask_session:
        pending = dict(flask_session[TWO_FACTOR_PENDING_KEY])
        pending["next"] = "https://evil.example/"
        flask_session[TWO_FACTOR_PENDING_KEY] = pending
    assert _second_factor(client, _now_code(secret)).headers["Location"] == "/"


def test_valid_next_survives_second_factor(tmp_path):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    client.post(LOGIN_FORM + "?next=/buchungen", data={"username": "maria", "password": PASSWORD})
    assert _second_factor(client, _now_code(secret)).headers["Location"] == "/buchungen"


def test_too_long_next_is_dropped_so_the_cookie_stays_valid(tmp_path):
    app = _create_app(tmp_path)
    _enable(app, _seed_user(app))
    client = app.test_client()
    client.post(
        LOGIN_FORM,
        query_string={"next": "/buchungen?state=" + "x" * 3000},
        data={"username": "maria", "password": PASSWORD},
    )
    with client.session_transaction() as flask_session:
        assert flask_session[TWO_FACTOR_PENDING_KEY]["next"] is None


def test_second_factor_post_requires_csrf_token(tmp_path):
    app = _create_app(tmp_path, CSRF_PROTECT=True)
    user_id = _seed_user(app)
    secret, _ = _enable(app, user_id)
    client = app.test_client()
    page = client.get(LOGIN_FORM).get_data(as_text=True)
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
    response = client.post(
        LOGIN_FORM, data={"username": "maria", "password": PASSWORD, "_csrf_token": csrf}
    )
    assert response.headers["Location"] == SECOND_FACTOR
    assert _second_factor(client, _now_code(secret)).status_code == 400
    page = client.get(SECOND_FACTOR).get_data(as_text=True)
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
    final = client.post(SECOND_FACTOR, data={"code": _now_code(secret), "_csrf_token": csrf})
    assert final.headers["Location"] == "/"


# ---------------------------------------------------------------------------
# OAuth über den Browser-Login
# ---------------------------------------------------------------------------

OAUTH_BASE = "https://obh.test"
OAUTH_REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


def _oauth_params(client) -> dict[str, str]:
    registered = client.post(
        "/oauth/register",
        json={
            "client_name": "ChatGPT",
            "redirect_uris": [OAUTH_REDIRECT],
            "token_endpoint_auth_method": "none",
        },
        base_url=OAUTH_BASE,
    ).get_json()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(b"v" * 43).digest()).rstrip(b"=").decode("ascii")
    )
    return {
        "response_type": "code",
        "client_id": registered["client_id"],
        "redirect_uri": OAUTH_REDIRECT,
        "state": "xyz",
        "scope": "mcp",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": f"{OAUTH_BASE}/mcp",
    }


def test_oauth_flow_cannot_finish_before_second_factor(tmp_path):
    app = _create_app(tmp_path)
    secret, _ = _enable(app, _seed_user(app))
    client = app.test_client()
    params = _oauth_params(client)

    anonymous = client.get("/oauth/authorize", query_string=params, base_url=OAUTH_BASE)
    login_url = anonymous.headers["Location"]
    assert LOGIN_FORM in login_url
    next_path = parse_qs(urlsplit(login_url).query)["next"][0]

    # Der Formular-Login führt in den zweiten Schritt, die Zustimmung bleibt verschlossen.
    response = client.post(
        LOGIN_FORM,
        query_string={"next": next_path},
        data={"username": "maria", "password": PASSWORD},
        base_url=OAUTH_BASE,
    )
    assert urlsplit(response.headers["Location"]).path == SECOND_FACTOR
    consent = client.get("/oauth/authorize", query_string=params, base_url=OAUTH_BASE)
    assert LOGIN_FORM in consent.headers["Location"]
    denied = client.post(
        "/oauth/authorize", data={**params, "decision": "allow"}, base_url=OAUTH_BASE
    )
    assert denied.status_code == 401

    # Nach dem zweiten Faktor geht es zurück in den OAuth-Flow.
    final = _second_factor(client, _now_code(secret), base_url=OAUTH_BASE)
    assert final.headers["Location"] == next_path
    assert client.get(next_path, base_url=OAUTH_BASE).status_code == 200
    allowed = client.post(
        "/oauth/authorize", data={**params, "decision": "allow"}, base_url=OAUTH_BASE
    )
    assert allowed.status_code == 302
    assert "code=" in allowed.headers["Location"]


# ---------------------------------------------------------------------------
# Wiederherstellungscodes
# ---------------------------------------------------------------------------


def test_recovery_code_works_exactly_once_and_is_stored_hashed(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    _, codes = _enable(app, user_id)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r"([A-HJ-NP-Z2-9]{4}-){3}[A-HJ-NP-Z2-9]{4}", code) for code in codes)
    with app.extensions["db_session_factory"]() as session:
        stored = {
            row.code_hash
            for row in session.execute(select(TwoFactorRecoveryCode)).scalars()
        }
    assert len(stored) == 10
    assert not any(code in value or code.replace("-", "") in value
                   for code in codes for value in stored)

    client = app.test_client()
    _login(client)
    # Tippfehlerfreundlich: Kleinbuchstaben und ohne Bindestriche.
    response = _second_factor(client, codes[0].replace("-", " ").lower())
    assert response.headers["Location"] == "/"
    assert "noch 9" in client.get("/").get_data(as_text=True)
    assert "two_factor_recovery_code_used" in _audit_actions(app)

    other = app.test_client()
    _login(other)
    assert _second_factor(other, codes[0]).headers["Location"] == SECOND_FACTOR
    assert "user" not in _session_keys(other)
    assert _second_factor(other, codes[1]).headers["Location"] == "/"


def test_recovery_codes_of_other_users_are_not_accepted(tmp_path):
    app = _create_app(tmp_path)
    _, codes_a = _enable(app, _seed_user(app, "anna"))
    _enable(app, _seed_user(app, "berta"))
    client = app.test_client()
    _login(client, "berta")
    assert _second_factor(client, codes_a[0]).headers["Location"] == SECOND_FACTOR


# ---------------------------------------------------------------------------
# Einrichtung in der Oberfläche
# ---------------------------------------------------------------------------


def test_setup_flow_in_ui(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client = app.test_client()
    _login(client)

    # Benutzermenü verlinkt die Seite.
    assert SETUP_PAGE in client.get("/").get_data(as_text=True)
    page = client.get(SETUP_PAGE)
    assert "Einrichtung beginnen" in page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-store"

    # Falsches Passwort: kein Secret.
    client.post(f"{SETUP_PAGE}/einrichten", data={"current_password": "falsch"})
    assert _user_state(app, user_id).totp_secret_encrypted is None

    assert (
        client.post(f"{SETUP_PAGE}/einrichten", data={"current_password": PASSWORD}).status_code
        == 302
    )
    state = _user_state(app, user_id)
    assert state.totp_secret_encrypted and state.totp_enabled_at is None

    page = client.get(SETUP_PAGE)
    body = page.get_data(as_text=True)
    assert page.headers["Cache-Control"] == "no-store"
    assert 'class="two-factor-qr"' in body and "Schlüssel:" in body
    # Strikte CSP: nur das eingebundene app.js, kein Inline-Skript.
    assert re.findall(r"<script(?![^>]*\ssrc=)", body) == []
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    with app.app_context():
        secret = decrypt_secret(state.totp_secret_encrypted)
    assert " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)) in body

    # Vor der Bestätigung gilt noch der Login ohne zweiten Faktor.
    assert _login(app.test_client()).headers["Location"] == "/"

    # Falscher Code aktiviert nicht.
    client.post(f"{SETUP_PAGE}/bestaetigen", data={"code": "000000"})
    assert _user_state(app, user_id).totp_enabled_at is None

    confirm_code = _now_code(secret)
    response = client.post(f"{SETUP_PAGE}/bestaetigen", data={"code": confirm_code})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_data(as_text=True)
    codes = re.findall(r"<li>([A-Z2-9-]{19})</li>", body)
    assert len(codes) == 10
    state = _user_state(app, user_id)
    assert state.totp_enabled_at is not None and state.totp_last_step is not None
    assert "two_factor_enabled" in _audit_actions(app)

    # Die Codes erscheinen nie wieder, auch das Secret nicht.
    body = client.get(SETUP_PAGE).get_data(as_text=True)
    assert codes[0] not in body and secret not in body and "two-factor-qr" not in body
    assert "10 Wiederherstellungscodes" in body

    # Danach fragt die Anmeldung den zweiten Faktor ab; der Code der Bestätigung ist verbraucht.
    fresh = app.test_client()
    assert _login(fresh).headers["Location"] == SECOND_FACTOR
    assert _second_factor(fresh, confirm_code).headers["Location"] == SECOND_FACTOR
    next_code = totp_code(secret, TOTP_NOW + 30)
    assert _second_factor(fresh, next_code).headers["Location"] == "/"


def test_setup_is_refused_while_active(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    secret, _ = _enable(app, user_id)
    client = app.test_client()
    _login(client)
    _second_factor(client, _now_code(secret))
    before = _user_state(app, user_id).totp_secret_encrypted
    client.post(f"{SETUP_PAGE}/einrichten", data={"current_password": PASSWORD})
    client.post(f"{SETUP_PAGE}/bestaetigen", data={"code": totp_code(secret, TOTP_NOW + 30)})
    assert _user_state(app, user_id).totp_secret_encrypted == before


def _logged_in_with_two_factor(app, user_id: int):
    secret, codes = _enable(app, user_id)
    client = app.test_client()
    _login(client)
    assert _second_factor(client, _now_code(secret)).headers["Location"] == "/"
    return client, secret, codes


def test_disable_requires_password_and_current_code(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client, secret, _ = _logged_in_with_two_factor(app, user_id)
    next_code = totp_code(secret, TOTP_NOW + 30)

    client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": "falsch", "code": next_code})
    assert _user_state(app, user_id).totp_enabled_at is not None
    client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD, "code": "000000"})
    assert _user_state(app, user_id).totp_enabled_at is not None
    # Nur Passwort oder nur Code genügt nicht; der bereits verbrauchte Code auch nicht.
    client.post(f"{SETUP_PAGE}/abschalten", data={"code": next_code})
    assert _user_state(app, user_id).totp_enabled_at is not None
    client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD})
    assert _user_state(app, user_id).totp_enabled_at is not None

    client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD, "code": next_code})
    state = _user_state(app, user_id)
    assert state.totp_enabled_at is None and state.totp_secret_encrypted is None
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(TwoFactorRecoveryCode)).scalars().all() == []
    assert "two_factor_disabled" in _audit_actions(app)
    assert _login(app.test_client()).headers["Location"] == "/"


def test_disable_with_recovery_code_after_losing_the_device(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client, _, codes = _logged_in_with_two_factor(app, user_id)
    client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD, "code": codes[0]})
    assert _user_state(app, user_id).totp_enabled_at is None


def test_regenerating_recovery_codes_invalidates_the_old_ones(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client, secret, old_codes = _logged_in_with_two_factor(app, user_id)

    rejected = client.post(
        f"{SETUP_PAGE}/codes", data={"current_password": "falsch", "code": old_codes[0]}
    )
    assert rejected.status_code == 302

    response = client.post(
        f"{SETUP_PAGE}/codes",
        data={"current_password": PASSWORD, "code": totp_code(secret, TOTP_NOW + 30)},
    )
    assert response.status_code == 200
    new_codes = re.findall(r"<li>([A-Z2-9-]{19})</li>", response.get_data(as_text=True))
    assert len(new_codes) == 10 and not set(new_codes) & set(old_codes)
    assert "two_factor_recovery_codes_regenerated" in _audit_actions(app)

    other = app.test_client()
    _login(other)
    assert _second_factor(other, old_codes[1]).headers["Location"] == SECOND_FACTOR
    assert _second_factor(other, new_codes[0]).headers["Location"] == "/"


def test_failed_attempts_on_sensitive_actions_count_in_rate_limit(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2)
    user_id = _seed_user(app)
    client, secret, _ = _logged_in_with_two_factor(app, user_id)
    for _ in range(2):
        client.post(f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD, "code": "0"})
    client.post(
        f"{SETUP_PAGE}/abschalten",
        data={"current_password": PASSWORD, "code": totp_code(secret, TOTP_NOW + 30)},
    )
    assert _user_state(app, user_id).totp_enabled_at is not None


# ---------------------------------------------------------------------------
# Pflicht je Rolle
# ---------------------------------------------------------------------------


def test_required_role_is_forced_onto_setup_page(tmp_path):
    app = _create_app(tmp_path, TWO_FACTOR_REQUIRED_ROLES="admin, Buchhalter")
    user_id = _seed_user(app, token="obk_maria")
    client = app.test_client()
    assert client.post(
        LOGIN_FORM + "?next=/buchungen", data={"username": "maria", "password": PASSWORD}
    ).headers["Location"] == "/buchungen"

    for path in ("/buchungen", "/", "/verwaltung", "/auth/password", "/auth/apps"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"] == SETUP_PAGE, path
    # Schreibende UI-Aktion ebenso, und die Session taugt nicht für die API.
    assert client.post("/tenants").headers["Location"] == SETUP_PAGE
    assert client.get("/api/v1/companies").status_code == 403

    page = client.get(SETUP_PAGE)
    body = page.get_data(as_text=True)
    assert page.status_code == 200 and "vorgeschrieben" in body
    assert 'class="mainnav"' not in body

    # API-Tokens bleiben ein eigener Zugang.
    assert client.get("/api/v1/users/me", headers=_bearer("obk_maria")).status_code == 200

    # Nach dem Einrichten ist der Weg frei, und es geht zum ursprünglichen Ziel weiter.
    client.post(f"{SETUP_PAGE}/einrichten", data={"current_password": PASSWORD})
    with app.app_context():
        secret = decrypt_secret(_user_state(app, user_id).totp_secret_encrypted)
    response = client.post(f"{SETUP_PAGE}/bestaetigen", data={"code": _now_code(secret)})
    body = response.get_data(as_text=True)
    assert 'class="button" href="/auth/apps"' in body
    assert 'class="mainnav"' in body
    assert client.get("/buchungen").status_code == 200
    assert client.get("/api/v1/companies").status_code == 200


def test_logout_stays_possible_while_setup_is_forced(tmp_path):
    app = _create_app(tmp_path, TWO_FACTOR_REQUIRED_ROLES="Buchhalter")
    _seed_user(app)
    client = app.test_client()
    _login(client)
    response = client.post("/auth/logout")
    assert response.headers["Location"] == LOGIN_FORM
    assert "user" not in _session_keys(client)


def test_other_roles_are_not_forced_and_default_is_empty(tmp_path):
    forced = _create_app(tmp_path, TWO_FACTOR_REQUIRED_ROLES="Admin")
    _seed_user(forced)
    client = forced.test_client()
    _login(client)
    assert client.get("/").status_code == 200

    assert _create_app(tmp_path).config["TWO_FACTOR_REQUIRED_ROLES"] == ""


def test_forced_user_cannot_finish_oauth_flow(tmp_path):
    app = _create_app(tmp_path, TWO_FACTOR_REQUIRED_ROLES="Buchhalter")
    _seed_user(app)
    client = app.test_client()
    params = _oauth_params(client)
    client.post(LOGIN_FORM, data={"username": "maria", "password": PASSWORD}, base_url=OAUTH_BASE)
    consent = client.get("/oauth/authorize", query_string=params, base_url=OAUTH_BASE)
    assert consent.headers["Location"] == SETUP_PAGE
    decision = client.post(
        "/oauth/authorize", data={**params, "decision": "allow"}, base_url=OAUTH_BASE
    )
    assert decision.status_code == 403


def test_existing_session_is_forced_when_requirement_applies_later(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = app.test_client()
    _login(client)
    assert client.get("/").status_code == 200
    app.config["TWO_FACTOR_REQUIRED_ROLES"] = "Buchhalter"
    assert client.get("/").headers["Location"] == SETUP_PAGE


def test_unknown_role_in_configuration_is_logged(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        _create_app(tmp_path, TWO_FACTOR_REQUIRED_ROLES="Admin,Buchalter")
    assert "Buchalter" in caplog.text
    assert "unbekannte Rollen" in caplog.text


def test_demo_mode_keeps_two_factor_unavailable(tmp_path):
    app = _create_app(tmp_path, DEMO_MODE=True, TWO_FACTOR_REQUIRED_ROLES="Buchhalter")
    user_id = _seed_user(app, token="obk_demo")
    client = app.test_client()
    _login(client)
    # Keine Rollenpflicht in der Demo.
    assert client.get("/").status_code == 200
    assert "nicht aktivieren" in client.get(SETUP_PAGE).get_data(as_text=True)

    for path in ("einrichten", "bestaetigen", "abschalten", "codes"):
        client.post(f"{SETUP_PAGE}/{path}", data={"current_password": PASSWORD})
    assert _user_state(app, user_id).totp_secret_encrypted is None

    blocked = client.post(
        "/api/v1/users/me/two-factor/setup",
        json={"current_password": PASSWORD},
        headers=_bearer("obk_demo"),
    )
    assert blocked.status_code == 403 and blocked.get_json()["demo"] is True

    with app.app_context(), app.extensions["db_session_factory"]() as session:
        with pytest.raises(TwoFactorError):
            start_setup(session, session.get(User, user_id), password=PASSWORD, actor="test")


# ---------------------------------------------------------------------------
# Administrator setzt zurück
# ---------------------------------------------------------------------------


def test_admin_reset_in_ui_shows_status_and_respects_tenant(tmp_path):
    app = _create_app(tmp_path)
    admin_a = _seed_user(app, "admin_a", role="Admin")
    target = _seed_user(app, "ziel")
    foreign = _seed_user(app, "fremd", tenant_name="Mandant B")
    _enable(app, admin_a)
    _enable(app, target)
    _enable(app, foreign)

    client = app.test_client()
    client.post(LOGIN_FORM, data={"username": "admin_a", "password": PASSWORD})
    client.post(
        SECOND_FACTOR,
        data={"code": _now_code(_secret_of(app, admin_a))},
    )
    page = client.get("/verwaltung").get_data(as_text=True)
    assert "<th>2FA</th>" in page and "2FA zurücksetzen" in page
    assert f"/users/{target}/two-factor/reset" in page
    # Für das eigene Konto gibt es keinen Knopf, fremde Mandanten erscheinen nicht.
    assert f"/users/{admin_a}/two-factor/reset" not in page
    assert f"/users/{foreign}/two-factor/reset" not in page

    client.post(f"/users/{foreign}/two-factor/reset")
    assert _user_state(app, foreign).totp_enabled_at is not None
    client.post(f"/users/{admin_a}/two-factor/reset")
    assert _user_state(app, admin_a).totp_enabled_at is not None

    assert client.post(f"/users/{target}/two-factor/reset").status_code == 302
    state = _user_state(app, target)
    assert state.totp_enabled_at is None and state.totp_secret_encrypted is None
    with app.extensions["db_session_factory"]() as session:
        remaining = session.execute(
            select(TwoFactorRecoveryCode).where(TwoFactorRecoveryCode.user_id == target)
        ).scalars()
        assert remaining.all() == []
    assert "two_factor_reset" in _audit_actions(app)
    assert _login(app.test_client(), "ziel").headers["Location"] == "/"


def _secret_of(app, user_id: int) -> str:
    with app.app_context():
        return decrypt_secret(_user_state(app, user_id).totp_secret_encrypted)


def test_non_admin_cannot_reset_in_ui(tmp_path):
    app = _create_app(tmp_path)
    target = _seed_user(app, "ziel")
    _enable(app, target)
    _seed_user(app, "kollegin")
    client = app.test_client()
    _login(client, "kollegin")
    client.post(f"/users/{target}/two-factor/reset")
    assert _user_state(app, target).totp_enabled_at is not None


def test_admin_reset_via_api_with_tenant_separation(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, "admin_a", role="Admin", token="obk_admin_a")
    _seed_user(app, "buchhalter", token="obk_buchhalter")
    _seed_user(app, "pruefer", role="Pruefer", token="obk_pruefer")
    _seed_user(app, "support", role="Support", tenant_name=None, token="obk_support")
    target = _seed_user(app, "ziel")
    foreign = _seed_user(app, "fremd", tenant_name="Mandant B")
    global_admin = _seed_user(
        app, "chefin", role="Admin", tenant_name=None, token="obk_global"
    )
    _enable(app, target)
    _enable(app, foreign)
    client = app.test_client()

    assert client.post(f"/api/v1/users/{target}/two-factor/reset").status_code == 401
    for token in ("obk_buchhalter", "obk_pruefer", "obk_support"):
        response = client.post(
            f"/api/v1/users/{target}/two-factor/reset", headers=_bearer(token)
        )
        assert response.status_code == 403, token
    assert client.post(
        f"/api/v1/users/{foreign}/two-factor/reset", headers=_bearer("obk_admin_a")
    ).status_code == 404
    assert _user_state(app, foreign).totp_enabled_at is not None
    assert client.post(
        "/api/v1/users/999/two-factor/reset", headers=_bearer("obk_global")
    ).status_code == 404

    # Das eigene Konto nur mit Passwort und Code, nicht über den Admin-Weg.
    assert client.post(
        f"/api/v1/users/{global_admin}/two-factor/reset", headers=_bearer("obk_global")
    ).status_code == 403

    response = client.post(
        f"/api/v1/users/{target}/two-factor/reset", headers=_bearer("obk_admin_a")
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["reset"] is True and body["two_factor"]["enabled"] is False

    repeated = client.post(
        f"/api/v1/users/{target}/two-factor/reset", headers=_bearer("obk_admin_a")
    )
    assert repeated.get_json()["reset"] is False

    assert client.post(
        f"/api/v1/users/{foreign}/two-factor/reset", headers=_bearer("obk_global")
    ).get_json()["reset"] is True


def test_cli_reset(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    _enable(app, user_id)
    runner = app.test_cli_runner()

    result = runner.invoke(args=["reset-2fa", "--username", "maria"])
    assert result.exit_code == 0 and "zurückgesetzt" in result.output
    assert _user_state(app, user_id).totp_enabled_at is None

    result = runner.invoke(args=["reset-2fa", "--username", "maria"])
    assert result.exit_code == 0 and "keine Zwei-Faktor" in result.output

    result = runner.invoke(args=["reset-2fa", "--username", "unbekannt"])
    assert result.exit_code != 0 and "nicht gefunden" in result.output


# ---------------------------------------------------------------------------
# REST-API
# ---------------------------------------------------------------------------


def test_api_setup_confirm_status_regenerate_and_disable(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    user_id = _seed_user(app, token="obk_maria")
    client = app.test_client()
    headers = _bearer("obk_maria")

    status = client.get("/api/v1/users/me/two-factor", headers=headers)
    assert status.status_code == 200
    assert status.get_json()["enabled"] is False
    assert status.get_json()["setup_pending"] is False

    assert client.post("/api/v1/users/me/two-factor/setup").status_code == 401
    wrong = client.post(
        "/api/v1/users/me/two-factor/setup", json={"current_password": "x"}, headers=headers
    )
    assert wrong.status_code == 403
    invalid = client.post(
        "/api/v1/users/me/two-factor/setup", json={"current_password": 1}, headers=headers
    )
    assert invalid.status_code == 403

    created = client.post(
        "/api/v1/users/me/two-factor/setup",
        json={"current_password": PASSWORD},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    body = created.get_json()
    secret = body["secret"]
    assert parse_qs(urlsplit(body["otpauth_uri"]).query)["secret"] == [secret]
    assert body["enabled"] is False and body["setup_pending"] is True

    status = client.get("/api/v1/users/me/two-factor", headers=headers).get_json()
    assert status["setup_pending"] is True and "secret" not in json.dumps(status).replace(
        "secret_readable", ""
    )

    assert (
        client.post(
            "/api/v1/users/me/two-factor/confirm", json={"code": "000000"}, headers=headers
        ).status_code
        == 403
    )
    confirmed = client.post(
        "/api/v1/users/me/two-factor/confirm",
        json={"code": _now_code(secret)},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.headers["Cache-Control"] == "no-store"
    codes = confirmed.get_json()["recovery_codes"]
    assert len(codes) == 10 and confirmed.get_json()["enabled"] is True

    assert (
        client.post(
            "/api/v1/users/me/two-factor/setup",
            json={"current_password": PASSWORD},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/users/me/two-factor/confirm",
            json={"code": _now_code(secret)},
            headers=headers,
        ).status_code
        == 409
    )

    # Nach dem Einschalten bleibt das API-Token ein eigener Zugang ohne zweiten Faktor.
    assert client.get("/api/v1/users/me", headers=headers).get_json()["auth"] == "user"

    regenerated = client.post(
        "/api/v1/users/me/two-factor/recovery-codes",
        json={"current_password": PASSWORD, "code": codes[0]},
        headers=headers,
    )
    assert regenerated.status_code == 200
    new_codes = regenerated.get_json()["recovery_codes"]
    assert not set(new_codes) & set(codes)

    denied = client.post(
        "/api/v1/users/me/two-factor/disable",
        json={"current_password": PASSWORD, "code": "000000"},
        headers=headers,
    )
    assert denied.status_code == 403
    disabled = client.post(
        "/api/v1/users/me/two-factor/disable",
        json={"current_password": PASSWORD, "code": new_codes[0]},
        headers=headers,
    )
    assert disabled.status_code == 200 and disabled.get_json()["enabled"] is False
    assert _user_state(app, user_id).totp_secret_encrypted is None

    # Abschalten ohne aktive 2FA ist ein Zustandsfehler.
    assert (
        client.post(
            "/api/v1/users/me/two-factor/disable",
            json={"current_password": PASSWORD, "code": new_codes[1]},
            headers=headers,
        ).status_code
        == 409
    )


def test_api_rate_limit_on_password_and_code(tmp_path):
    app = _create_app(
        tmp_path, API_REQUIRE_AUTH=True, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2
    )
    _seed_user(app, token="obk_maria")
    client = app.test_client()
    headers = _bearer("obk_maria")
    for _ in range(2):
        assert (
            client.post(
                "/api/v1/users/me/two-factor/setup",
                json={"current_password": "falsch"},
                headers=headers,
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/api/v1/users/me/two-factor/setup",
            json={"current_password": PASSWORD},
            headers=headers,
        ).status_code
        == 429
    )


def test_api_status_rights_and_scope(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True, API_AUTH_TOKEN="global-backend-token")
    admin = _seed_user(app, "admin_a", role="Admin", token="obk_admin_a")
    target = _seed_user(app, "ziel", token="obk_ziel")
    foreign = _seed_user(app, "fremd", tenant_name="Mandant B")
    _enable(app, target)
    client = app.test_client()
    url = "/api/v1/users/me/two-factor"

    status = client.get(url, headers=_bearer("obk_ziel")).get_json()
    assert status["enabled"] is True
    assert status["recovery_codes_remaining"] == 10
    assert status["secret_readable"] is True
    assert "totp_secret_encrypted" not in status

    # Administrator sieht Benutzer des eigenen Mandanten.
    other = client.get(url, query_string={"user_id": target}, headers=_bearer("obk_admin_a"))
    assert other.status_code == 200 and other.get_json()["username"] == "ziel"
    assert client.get(
        url, query_string={"user_id": foreign}, headers=_bearer("obk_admin_a")
    ).status_code == 404

    # Andere dürfen fremde Stati nicht sehen, den eigenen auch per ID.
    assert client.get(
        url, query_string={"user_id": admin}, headers=_bearer("obk_ziel")
    ).status_code == 403
    assert client.get(
        url, query_string={"user_id": target}, headers=_bearer("obk_ziel")
    ).status_code == 200
    assert client.get(
        url, query_string={"user_id": "abc"}, headers=_bearer("obk_ziel")
    ).status_code == 400

    # Der globale Token hat keinen eigenen Benutzer, kann aber abfragen.
    global_headers = _bearer("global-backend-token")
    assert client.get(url, headers=global_headers).status_code == 400
    assert client.get(
        url, query_string={"user_id": target}, headers=global_headers
    ).status_code == 200
    assert client.post(
        f"{url}/setup", json={"current_password": PASSWORD}, headers=global_headers
    ).status_code == 400

    # In der Benutzerliste steht der Status, nicht das Secret.
    listing = client.get("/api/v1/users", headers=_bearer("obk_admin_a")).get_json()["users"]
    row = next(item for item in listing if item["id"] == target)
    assert row["two_factor"]["enabled"] is True
    assert "totp" not in json.dumps(listing)


def test_session_can_read_status_but_not_change(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app)
    client = app.test_client()
    _login(client)
    assert client.get("/api/v1/users/me/two-factor").status_code == 200
    assert client.post(
        "/api/v1/users/me/two-factor/setup", json={"current_password": PASSWORD}
    ).status_code == 401


# ---------------------------------------------------------------------------
# MCP und Chat
# ---------------------------------------------------------------------------


def _mcp_tool(client, name: str, arguments: dict, headers=None) -> dict:
    response = client.post(
        "/api/v1/mcp/call",
        json={"method": "tools/call", "params": {"name": name, "arguments": arguments}},
        headers=headers,
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


def test_mcp_only_offers_status_and_reset_and_chat_blocks_both(tmp_path):
    names = {tool.name for tool in TOOLS if "two_factor" in tool.name or "totp" in tool.name}
    assert names == {"get_user_two_factor_status", "reset_user_two_factor"}
    assert all(chat_tool_blocked(name) for name in names)
    # Kein Tool berührt Einrichten, Bestätigen, Abschalten oder Codes.
    paths = {tool.path for tool in TOOLS if "two-factor" in tool.path}
    assert paths == {"/users/me/two-factor", "/users/{user_id}/two-factor/reset"}


def test_mcp_status_and_reset(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, "admin_a", role="Admin", token="obk_admin_a")
    target = _seed_user(app, "ziel", token="obk_ziel")
    foreign = _seed_user(app, "fremd", tenant_name="Mandant B")
    secret, codes = _enable(app, target)
    _enable(app, foreign)
    client = app.test_client()
    admin = _bearer("obk_admin_a")
    own = _bearer("obk_ziel")

    result = _mcp_tool(client, "get_user_two_factor_status", {}, headers=own)
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert json.loads(text)["enabled"] is True
    assert secret not in text and codes[0] not in text

    result = _mcp_tool(client, "get_user_two_factor_status", {"user_id": target}, headers=admin)
    assert json.loads(result["content"][0]["text"])["username"] == "ziel"
    result = _mcp_tool(client, "get_user_two_factor_status", {"user_id": foreign}, headers=admin)
    assert result["isError"] is True

    assert _mcp_tool(client, "reset_user_two_factor", {"user_id": target}, headers=own)["isError"]
    result = _mcp_tool(client, "reset_user_two_factor", {"user_id": foreign}, headers=admin)
    assert result["isError"] is True
    assert _user_state(app, foreign).totp_enabled_at is not None
    result = _mcp_tool(client, "reset_user_two_factor", {"user_id": target}, headers=admin)
    assert result["isError"] is False
    assert _user_state(app, target).totp_enabled_at is None


# ---------------------------------------------------------------------------
# Keine Geheimnisse in Log, Audit-Log und Prüferexport
# ---------------------------------------------------------------------------


def test_secrets_and_codes_never_reach_log_or_audit(tmp_path, caplog):
    app = _create_app(tmp_path)
    user_id = _seed_user(app, token="obk_maria")
    client = app.test_client()
    with caplog.at_level(logging.DEBUG):
        _login(client)
        client.post(f"{SETUP_PAGE}/einrichten", data={"current_password": PASSWORD})
        secret = _secret_of(app, user_id)
        response = client.post(f"{SETUP_PAGE}/bestaetigen", data={"code": _now_code(secret)})
        codes = re.findall(r"<li>([A-Z2-9-]{19})</li>", response.get_data(as_text=True))
        other = app.test_client()
        _login(other)
        _second_factor(other, "000000")
        _second_factor(other, codes[0])
        client.post(
            f"{SETUP_PAGE}/abschalten", data={"current_password": PASSWORD, "code": codes[1]}
        )

    with app.extensions["db_session_factory"]() as session:
        audit = json.dumps(
            [
                [row.action, row.payload, row.changed_by]
                for row in session.execute(select(AuditLog)).scalars()
            ]
        )
    needles = [secret, *codes, *[code.replace("-", "") for code in codes]]
    for haystack in (caplog.text, audit):
        for needle in needles:
            assert needle not in haystack
    actions = _audit_actions(app)
    for expected in (
        "two_factor_enabled",
        "two_factor_failed",
        "two_factor_verified",
        "two_factor_recovery_code_used",
        "two_factor_disabled",
    ):
        assert expected in actions, expected
    assert "action=two_factor_setup_started" in caplog.text


def test_audit_export_contains_no_two_factor_data(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    secret, codes = _enable(app, user_id)
    with app.extensions["db_session_factory"]() as session:
        tenant_id = session.get(User, user_id).tenant_id
        company = Company(name="Muster GmbH", currency_code="EUR", tenant_id=tenant_id)
        session.add(company)
        session.commit()
        encrypted = session.get(User, user_id).totp_secret_encrypted
        package = build_audit_export_package(
            session=session,
            company_id=company.id,
            generated_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
            include_documents=False,
        )

    assert {"totp_secret_encrypted", "totp_enabled_at", "totp_last_step"} <= (
        EXCLUDED_EXPORT_FIELDS["users"]
    )
    with zipfile.ZipFile(BytesIO(package.payload)) as archive:
        contents = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
    joined = "\n".join(contents.values())
    for needle in (secret, encrypted, codes[0], "totp_", "two_factor_recovery_code", "code_hash"):
        assert needle not in joined, needle
    assert '"username": "maria"' in contents["data/users.json"]


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_round_trip(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'roundtrip.db'}")
    config = _alembic_config(engine)
    command.upgrade(config, "head")

    def schema() -> tuple[set[str], set[str]]:
        inspector = inspect(engine)
        return (
            {column["name"] for column in inspector.get_columns("user")},
            set(inspector.get_table_names()),
        )

    columns, tables = schema()
    assert {"totp_secret_encrypted", "totp_enabled_at", "totp_last_step"} <= columns
    assert "two_factor_recovery_code" in tables

    script = ScriptDirectory.from_config(config)
    previous = script.get_revision("20261103_0060").down_revision
    command.downgrade(config, previous)
    columns, tables = schema()
    assert not {"totp_secret_encrypted", "totp_enabled_at", "totp_last_step"} & columns
    assert "two_factor_recovery_code" not in tables

    command.upgrade(config, "head")
    columns, tables = schema()
    assert "totp_secret_encrypted" in columns and "two_factor_recovery_code" in tables
