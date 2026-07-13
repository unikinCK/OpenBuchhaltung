"""Tests für das Security-Inkrement: API-Auth-Default, Token-Lookup, Rate-Limit."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app import create_app
from app.auth import generate_api_token, hash_api_token, hash_password, reset_login_rate_limiter
from domain.models import Tenant, User


@pytest.fixture(autouse=True)
def _clean_rate_limiter():
    reset_login_rate_limiter()
    yield
    reset_login_rate_limiter()


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'security.db'}",
    }
    config.update(overrides)
    return create_app(config)


def _seed_user(
    app,
    *,
    username: str = "admin",
    password: str = "geheim",
    role: str = "Admin",
    tenant: bool = False,
    api_token_hash: str | None = None,
    is_active: bool = True,
) -> int:
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        tenant_id = None
        if tenant:
            tenant_row = Tenant(name=f"Tenant {username}")
            session.add(tenant_row)
            session.flush()
            tenant_id = tenant_row.id
        user = User(
            username=username,
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
            api_token_hash=api_token_hash,
            is_active=is_active,
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client, username: str = "admin", password: str = "geheim"):
    return client.post(
        "/auth/login", data={"username": username, "password": password}, follow_redirects=True
    )


def test_api_requires_auth_by_default_outside_testing(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    client = app.test_client()

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/companies").status_code == 401
    assert client.post("/api/v1/tenants", json={}).status_code == 401


def test_env_default_is_secure(tmp_path: Path, monkeypatch) -> None:
    """Ohne TESTING und ohne API_REQUIRE_AUTH-Env ist die API zu."""
    monkeypatch.delenv("API_REQUIRE_AUTH", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'env.db'}")
    app = create_app()
    assert app.config["API_REQUIRE_AUTH"] is True


def test_production_start_requires_secret_key(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


def test_inactive_user_loses_existing_browser_session(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    client = app.test_client()
    _login(client)
    assert client.get("/").status_code == 200

    with app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        user.is_active = False
        session.commit()

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login?next=/")


def test_role_changes_apply_to_existing_browser_session(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    user_id = _seed_user(app, role="Admin")
    client = app.test_client()
    _login(client)

    with app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        user.role = "Pruefer"
        session.commit()

    response = client.post("/tenants", data={"tenant_name": "X", "company_name": "Y"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_logged_in_session_gets_read_only_api_access(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app)
    client = app.test_client()
    _login(client)

    # Lesende API-Zugriffe (z. B. Berichte-Downloadlinks) funktionieren per Session.
    assert client.get("/api/v1/companies").status_code == 200

    # Schreibende API-Zugriffe brauchen weiterhin ein Bearer-Token (kein CSRF-Risiko).
    response = client.post(
        "/api/v1/tenants", json={"tenant_name": "X", "company_name": "Y"}
    )
    assert response.status_code == 401


def test_session_api_access_is_tenant_scoped(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, username="mandant-admin", role="Admin", tenant=True)

    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        session.add(Tenant(name="Fremder Tenant"))
        session.commit()

    client = app.test_client()
    _login(client, username="mandant-admin")
    payload = client.get("/api/v1/companies").get_json()
    # Der Session-User sieht nur den eigenen (leeren) Tenant, nicht den fremden.
    assert payload == []


def test_new_api_tokens_are_stored_as_sha256_and_looked_up(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    token = generate_api_token()
    _seed_user(app, api_token_hash=hash_api_token(token))

    digest = hash_api_token(token)
    assert len(digest) == 64 and "$" not in digest

    client = app.test_client()
    response = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_legacy_scrypt_token_still_works_and_is_migrated(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    token = generate_api_token()
    user_id = _seed_user(app, api_token_hash=generate_password_hash(token))

    client = app.test_client()
    response = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200

    # Lazy-Migration: Der Hash liegt jetzt als SHA-256 vor.
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        stored = session.execute(
            select(User.api_token_hash).where(User.id == user_id)
        ).scalar_one()
    assert stored == hash_api_token(token)

    # Und der schnelle Lookup funktioniert weiterhin.
    response = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_inactive_user_token_is_rejected(tmp_path: Path) -> None:
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    token = generate_api_token()
    _seed_user(app, api_token_hash=hash_api_token(token), is_active=False)

    client = app.test_client()
    response = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_login_rate_limit_blocks_after_max_attempts(tmp_path: Path) -> None:
    app = _create_app(
        tmp_path,
        LOGIN_RATE_LIMIT=True,
        LOGIN_RATE_LIMIT_ATTEMPTS=3,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    _seed_user(app)
    client = app.test_client()

    for _ in range(3):
        response = client.post(
            "/auth/login", data={"username": "admin", "password": "falsch"}
        )
        assert response.status_code == 302  # Redirect zurück zum Login

    blocked = client.post("/auth/login", data={"username": "admin", "password": "falsch"})
    assert blocked.status_code == 429

    # Auch mit korrektem Passwort bleibt die Sperre im Fenster aktiv.
    blocked = client.post("/auth/login", data={"username": "admin", "password": "geheim"})
    assert blocked.status_code == 429


def test_login_rate_limit_resets_after_successful_login(tmp_path: Path) -> None:
    app = _create_app(
        tmp_path,
        LOGIN_RATE_LIMIT=True,
        LOGIN_RATE_LIMIT_ATTEMPTS=3,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    _seed_user(app)
    client = app.test_client()

    for _ in range(2):
        client.post("/auth/login", data={"username": "admin", "password": "falsch"})

    success = _login(client)
    assert "Login erfolgreich" in success.get_data(as_text=True)

    # Zähler ist zurückgesetzt: erneute Fehlversuche starten bei 0.
    for _ in range(2):
        response = client.post(
            "/auth/login", data={"username": "admin", "password": "falsch"}
        )
        assert response.status_code == 302


def test_login_rate_limit_is_per_username(tmp_path: Path) -> None:
    app = _create_app(
        tmp_path,
        LOGIN_RATE_LIMIT=True,
        LOGIN_RATE_LIMIT_ATTEMPTS=2,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS=900,
    )
    _seed_user(app)
    _seed_user(app, username="buchhalter", role="Buchhalter")
    client = app.test_client()

    for _ in range(2):
        client.post("/auth/login", data={"username": "admin", "password": "falsch"})
    assert (
        client.post("/auth/login", data={"username": "admin", "password": "falsch"}).status_code
        == 429
    )

    # Anderer Benutzername ist nicht gesperrt.
    response = client.post(
        "/auth/login", data={"username": "buchhalter", "password": "falsch"}
    )
    assert response.status_code == 302
