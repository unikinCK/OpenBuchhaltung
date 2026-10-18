"""Regressionstests Sprint 2 „Sicherheit“ (S3–S10, Audit-Events, Passwort ändern)."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import select
from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app
from app.auth import (
    constant_time_equals,
    generate_api_token,
    hash_api_token,
    hash_password,
    safe_next_path,
)
from app.services import chat_llm
from app.services.chat_llm import ChatLLMError
from app.services.mcp_http import authorization_allowed, make_server
from app.services.mcp_server import ApiResponse, MCPServer
from domain.models import AuditLog, Company, LoginAttempt, Tenant, User


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'sprint2.db'}",
    }
    config.update(overrides)
    return create_app(config)


def _seed_user(
    app,
    *,
    username: str = "admin",
    password: str = "geheim123",
    role: str = "Admin",
    tenant_name: str | None = None,
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
            password_hash=hash_password(password),
            role=role,
            tenant_id=tenant_id,
            api_token_hash=hash_api_token(token) if token else None,
            api_token_last4=token[-4:] if token else None,
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client, username: str = "admin", password: str = "geheim123", **kwargs):
    return client.post(
        "/auth/login", data={"username": username, "password": password}, **kwargs
    )


def _audit_actions(app, *, entity_type: str = "user") -> list[str]:
    with app.extensions["db_session_factory"]() as session:
        rows = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == entity_type)
                .order_by(AuditLog.sequence_number)
            )
            .scalars()
            .all()
        )
        return [row.action for row in rows]


# ---------------------------------------------------------------------------
# S3: MCP-Bridge in-process mit Aufruferkontext
# ---------------------------------------------------------------------------


def test_mcp_bridge_uses_caller_context_for_scoping(tmp_path):
    token = "obk_tenant-buchhalter"
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, username="buchhalter", role="Buchhalter", tenant_name="Mandant A",
               token=token)
    with app.extensions["db_session_factory"]() as session:
        foreign = Tenant(name="Fremd")
        session.add(foreign)
        session.flush()
        session.add(Company(name="Fremd GmbH", currency_code="EUR", tenant_id=foreign.id))
        session.commit()
    client = app.test_client()
    headers = {"Authorization": f"Bearer {token}"}

    def bridge(name: str, arguments: dict) -> dict:
        response = client.post(
            "/api/v1/mcp/call",
            headers=headers,
            json={"id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}},
        )
        assert response.status_code == 200
        return response.get_json()["result"]

    # Fremde Mandanten bleiben unsichtbar, Admin-Funktionen sind verboten.
    companies = json.loads(bridge("list_companies", {})["content"][0]["text"])
    assert companies == []
    users = bridge("list_users", {})
    assert users["isError"] is True and "Forbidden" in users["content"][0]["text"]
    created = bridge("create_user", {"username": "x", "password": "geheim123"})
    assert created["isError"] is True
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(User).where(User.username == "x")).first() is None

    # Ohne gültiges Token gibt es die Bridge gar nicht.
    assert client.post("/api/v1/mcp/call", json={"method": "tools/list"}).status_code == 401


def test_mcp_bridge_answers_unknown_method_with_jsonrpc_error(tmp_path):
    app = _create_app(tmp_path)
    response = app.test_client().post(
        "/api/v1/mcp/call", json={"id": 3, "method": "resources/list", "params": {}}
    )
    assert response.status_code == 200
    assert response.get_json()["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# S4: ProxyFix, Rate-Limit in der DB, Admin-Entsperrung
# ---------------------------------------------------------------------------


def test_login_rate_limit_is_persisted_in_database(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2)
    _seed_user(app)
    client = app.test_client()

    for _ in range(2):
        assert _login(client, password="falsch").status_code == 302
    with app.extensions["db_session_factory"]() as session:
        attempts = session.execute(select(LoginAttempt)).scalars().all()
        assert len(attempts) == 2
        assert {row.username for row in attempts} == {"admin"}
    assert _login(client, password="falsch").status_code == 429

    # Ein anderer Prozess (neue App-Instanz auf derselben DB) sieht die Sperre auch.
    other_process = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2)
    assert _login(other_process.test_client(), password="falsch").status_code == 429


def test_proxyfix_separates_clients_behind_reverse_proxy(tmp_path):
    app = _create_app(
        tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2, TRUSTED_PROXY_COUNT=1
    )
    _seed_user(app)
    client = app.test_client()
    attacker = {"X-Forwarded-For": "203.0.113.1"}
    victim = {"X-Forwarded-For": "203.0.113.2"}

    assert isinstance(app.wsgi_app, ProxyFix)
    for _ in range(2):
        _login(client, password="falsch", headers=attacker)
    assert _login(client, password="falsch", headers=attacker).status_code == 429
    # Anderer Client hinter demselben Proxy ist nicht gesperrt.
    assert _login(client, headers=victim).status_code == 302
    with app.extensions["db_session_factory"]() as session:
        assert {row.remote_addr for row in session.execute(select(LoginAttempt)).scalars()} == {
            "203.0.113.1"
        }


def test_forwarded_headers_are_ignored_without_trusted_proxy(tmp_path):
    app = _create_app(
        tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=2, TRUSTED_PROXY_COUNT=0
    )
    _seed_user(app)
    client = app.test_client()
    assert not isinstance(app.wsgi_app, ProxyFix)

    for _ in range(2):
        _login(client, password="falsch", headers={"X-Forwarded-For": "203.0.113.1"})
    # Gefälschte Forwarded-Header helfen nicht: dieselbe echte Adresse bleibt gesperrt.
    blocked = _login(client, password="falsch", headers={"X-Forwarded-For": "203.0.113.9"})
    assert blocked.status_code == 429


def test_admin_can_unlock_login_via_ui_and_api(tmp_path):
    app = _create_app(tmp_path, LOGIN_RATE_LIMIT=True, LOGIN_RATE_LIMIT_ATTEMPTS=1)
    _seed_user(app, username="admin", role="Admin")
    user_id = _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()

    _login(client, username="maria", password="falsch")
    assert _login(client, username="maria").status_code == 429

    # Entsperrung per UI durch den Admin.
    admin = app.test_client()
    _login(admin)
    unlocked = admin.post(f"/users/{user_id}/unlock", follow_redirects=True)
    assert unlocked.status_code == 200
    assert "Login-Sperre wurde aufgehoben".encode() in unlocked.data
    assert _login(client, username="maria").status_code == 302

    # … und per API.
    _login(client, username="maria", password="falsch")
    assert _login(client, username="maria").status_code == 429
    api = app.test_client().post(f"/api/v1/users/{user_id}/unlock")
    assert api.status_code == 200
    assert api.get_json()["removed_attempts"] == 1
    assert _login(client, username="maria").status_code == 302
    assert "login_unlocked" in _audit_actions(app)


def test_production_defaults_enable_proxyfix_secure_cookie_and_hsts(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "prod-secret")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'prod.db'}")
    for name in ("SESSION_COOKIE_SECURE", "TRUSTED_PROXY_COUNT", "HSTS_ENABLED"):
        monkeypatch.delenv(name, raising=False)

    app = create_app()

    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["HSTS_ENABLED"] is True
    assert app.config["TRUSTED_PROXY_COUNT"] == 1
    assert isinstance(app.wsgi_app, ProxyFix)
    assert app.permanent_session_lifetime == timedelta(hours=8)


def test_development_defaults_stay_relaxed(tmp_path):
    app = _create_app(tmp_path)
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["HSTS_ENABLED"] is False
    assert not isinstance(app.wsgi_app, ProxyFix)


# ---------------------------------------------------------------------------
# S6: Open Redirect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "/\\evil.example",
        "//evil.example",
        "/\\\\evil.example/",
        "https://evil.example/",
        "javascript:alert(1)",
        " /journal",
        "/journal\r\nSet-Cookie: x=1",
        "",
        None,
    ],
)
def test_safe_next_path_rejects_external_targets(candidate):
    assert safe_next_path(candidate) is None


@pytest.mark.parametrize("candidate", ["/", "/journal?company_id=1", "/belege#top"])
def test_safe_next_path_accepts_relative_paths(candidate):
    assert safe_next_path(candidate) == candidate


def test_login_ignores_backslash_open_redirect(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = app.test_client()

    response = client.post(
        "/auth/login?next=/\\evil.example",
        data={"username": "admin", "password": "geheim123"},
    )
    assert response.status_code == 302
    assert response.headers["Location"] in {"/", "http://localhost/"}

    response = client.post(
        "/auth/login?next=/verwaltung", data={"username": "admin", "password": "geheim123"}
    )
    assert response.headers["Location"].endswith("/verwaltung")


# ---------------------------------------------------------------------------
# S7: Cookie-Härtung, HSTS, Session-Laufzeit
# ---------------------------------------------------------------------------


def test_hsts_header_only_on_https(tmp_path):
    app = _create_app(tmp_path, HSTS_ENABLED=True, HSTS_MAX_AGE=1234)
    client = app.test_client()

    secure = client.get("/api/v1/health", base_url="https://localhost")
    assert secure.headers["Strict-Transport-Security"] == "max-age=1234; includeSubDomains"
    plain = client.get("/api/v1/health", base_url="http://localhost")
    assert "Strict-Transport-Security" not in plain.headers


def test_hsts_honours_forwarded_proto_behind_proxy(tmp_path):
    app = _create_app(tmp_path, HSTS_ENABLED=True, TRUSTED_PROXY_COUNT=1)
    response = app.test_client().get(
        "/api/v1/health", headers={"X-Forwarded-Proto": "https"}
    )
    assert "Strict-Transport-Security" in response.headers


def test_login_starts_permanent_session_with_lifetime(tmp_path):
    app = _create_app(tmp_path, PERMANENT_SESSION_LIFETIME=timedelta(minutes=30))
    _seed_user(app)
    client = app.test_client()

    response = _login(client)
    cookie = response.headers["Set-Cookie"]
    assert "Expires=" in cookie or "Max-Age=" in cookie
    assert app.permanent_session_lifetime == timedelta(minutes=30)


# ---------------------------------------------------------------------------
# S8: Mandantenanlage in der UI nur für globale Admins
# ---------------------------------------------------------------------------


def test_global_buchhalter_cannot_create_tenant_via_ui(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, username="global-buchhalter", role="Buchhalter")
    client = app.test_client()
    _login(client, username="global-buchhalter")

    response = client.post(
        "/tenants",
        data={"tenant_name": "Neu", "company_name": "Neu GmbH"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"globaler Administrator" in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(Tenant).where(Tenant.name == "Neu")).first() is None


# ---------------------------------------------------------------------------
# S9: MCP-HTTP Body-Limit
# ---------------------------------------------------------------------------


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        self.calls.append((method, path, params, json_body))
        return ApiResponse(status=200, text="{}", content_type="application/json", json={})


def test_mcp_http_rejects_oversized_bodies():
    import threading

    httpd = make_server(
        MCPServer(http=_RecordingHttp()), host="127.0.0.1", port=0, path="/mcp",
        max_body_bytes=64,
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
        small = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        with urlopen(Request(url, data=small, method="POST"), timeout=5) as response:
            assert response.status == 200
        big = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": "a" * 100}}
        ).encode()
        with pytest.raises(HTTPError) as excinfo:
            urlopen(Request(url, data=big, method="POST"), timeout=5)
        assert excinfo.value.code == 413
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_http_authorization_tolerates_non_ascii_tokens():
    assert authorization_allowed("Bearer tökén", "secret") is False
    assert authorization_allowed("Bearer secret", "secret") is True


# ---------------------------------------------------------------------------
# S10: Einzeiler
# ---------------------------------------------------------------------------


def test_constant_time_equals_handles_non_ascii():
    assert constant_time_equals("tökén", "tökén") is True
    assert constant_time_equals("tökén", "token") is False


def test_non_ascii_bearer_token_yields_401_not_500(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True, API_AUTH_TOKEN="secret")
    response = app.test_client().get(
        "/api/v1/companies", headers={"Authorization": "Bearer tökén"}
    )
    assert response.status_code == 401


def test_non_ascii_csrf_token_yields_400_not_500(tmp_path):
    app = _create_app(tmp_path, CSRF_PROTECT=True)
    _seed_user(app)
    client = app.test_client()
    client.get("/auth/login")
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "geheim123", "_csrf_token": "tökén"},
    )
    assert response.status_code == 400


def test_chat_llm_http_error_hides_upstream_body(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise HTTPError(
            request.full_url, 500, "boom", None, io.BytesIO(b"internal secret trace")
        )

    monkeypatch.setattr(chat_llm, "urlopen", fake_urlopen)
    with pytest.raises(ChatLLMError) as excinfo:
        chat_llm._post_json("http://llm.test/v1/responses", {"input": []})
    assert "HTTP 500" in str(excinfo.value)
    assert "internal secret" not in str(excinfo.value)


def test_cli_create_user_prompts_for_password_and_enforces_policy(tmp_path):
    app = _create_app(tmp_path)
    runner = app.test_cli_runner()

    prompted = runner.invoke(
        args=["create-user", "--username", "maria"], input="geheim123\ngeheim123\n"
    )
    assert prompted.exit_code == 0, prompted.output
    assert "Benutzer maria (Buchhalter) wurde angelegt." in prompted.output

    short = runner.invoke(args=["create-user", "--username", "kurz", "--password", "abc"])
    assert short.exit_code != 0
    assert "mindestens 8 Zeichen" in short.output


def test_cli_set_password_updates_login(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    runner = app.test_cli_runner()

    result = runner.invoke(
        args=["set-password", "--username", "maria"], input="neuesgeheim\nneuesgeheim\n"
    )
    assert result.exit_code == 0, result.output
    client = app.test_client()
    assert _login(client, username="maria", password="geheim123").status_code == 302
    assert "Login erfolgreich" not in _login(
        client, username="maria", password="geheim123", follow_redirects=True
    ).get_data(as_text=True)
    assert "Login erfolgreich" in _login(
        client, username="maria", password="neuesgeheim", follow_redirects=True
    ).get_data(as_text=True)
    assert "password_reset" in _audit_actions(app)


def test_seed_demo_is_refused_in_production(tmp_path):
    app = _create_app(tmp_path, APP_ENV="production", SECRET_KEY="prod-secret")
    app.config["TESTING"] = False
    result = app.test_cli_runner().invoke(args=["seed-demo"])
    assert result.exit_code != 0
    assert "Produktion" in result.output
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(User)).first() is None


def test_api_token_is_shown_once_and_not_flashed_into_session(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    user_id = _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()
    _login(client)

    response = client.post(f"/users/{user_id}/api-token")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "API-Token für maria" in body and "obk_" in body
    with client.session_transaction() as flask_session:
        assert "obk_" not in json.dumps(flask_session.get("_flashes", []))
    assert client.get("/verwaltung").status_code == 200
    assert "obk_" not in client.get("/verwaltung").get_data(as_text=True)


# ---------------------------------------------------------------------------
# Audit-Events und Passwort-ändern-Flow
# ---------------------------------------------------------------------------


def test_login_events_are_written_to_audit_chain(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()

    _login(client, username="maria", password="falsch")
    _login(client, username="maria")
    _login(client, username="unbekannt", password="x")

    assert _audit_actions(app) == ["login_failed", "login"]
    with app.extensions["db_session_factory"]() as session:
        entries = session.execute(select(AuditLog)).scalars().all()
        assert all(entry.changed_by == "maria" for entry in entries)
        assert all(entry.payload.get("remote_addr") for entry in entries)


def test_user_admin_actions_are_audited_without_leaking_secrets(tmp_path):
    app = _create_app(tmp_path)
    client = app.test_client()
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Mandant A")
        session.add(tenant)
        session.commit()
        tenant_id = tenant.id

    created = client.post(
        "/api/v1/users",
        json={"username": "maria", "password": "geheim123", "role": "Buchhalter",
              "tenant_id": tenant_id},
    )
    assert created.status_code == 201
    user_id = created.get_json()["id"]
    rotated = client.post(f"/api/v1/users/{user_id}/api-token", json={})
    token = rotated.get_json()["api_token"]
    client.post(f"/api/v1/users/{user_id}/active", json={"is_active": False})
    client.post(f"/api/v1/users/{user_id}/password", json={"new_password": "nochgeheimer"})

    assert _audit_actions(app) == [
        "created", "api_token_rotated", "deactivated", "password_reset"
    ]
    with app.extensions["db_session_factory"]() as session:
        dump = json.dumps(
            [entry.payload for entry in session.execute(select(AuditLog)).scalars()]
        )
    assert token not in dump
    assert "geheim123" not in dump and "nochgeheimer" not in dump
    assert token[-4:] in dump


def test_api_rejects_short_passwords(tmp_path):
    app = _create_app(tmp_path)
    response = app.test_client().post(
        "/api/v1/users", json={"username": "kurz", "password": "abc", "role": "Pruefer"}
    )
    assert response.status_code == 400
    assert "mindestens 8 Zeichen" in response.get_json()["error"]


def test_password_change_via_ui(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()
    _login(client, username="maria")

    page = client.get("/auth/password")
    assert page.status_code == 200
    assert "Passwort ändern".encode() in page.data

    wrong = client.post(
        "/auth/password",
        data={"current_password": "falsch", "new_password": "neuesgeheim",
              "confirm_password": "neuesgeheim"},
        follow_redirects=True,
    )
    assert "aktuelle Passwort ist falsch".encode() in wrong.data

    mismatch = client.post(
        "/auth/password",
        data={"current_password": "geheim123", "new_password": "neuesgeheim",
              "confirm_password": "anders"},
        follow_redirects=True,
    )
    assert "Wiederholung".encode() in mismatch.data

    changed = client.post(
        "/auth/password",
        data={"current_password": "geheim123", "new_password": "neuesgeheim",
              "confirm_password": "neuesgeheim"},
        follow_redirects=True,
    )
    assert "Passwort wurde ge".encode() in changed.data

    fresh = app.test_client()
    assert "Login erfolgreich" not in _login(
        fresh, username="maria", password="geheim123", follow_redirects=True
    ).get_data(as_text=True)
    assert "Login erfolgreich" in _login(
        fresh, username="maria", password="neuesgeheim", follow_redirects=True
    ).get_data(as_text=True)
    assert "password_changed" in _audit_actions(app)


def test_password_page_requires_login(tmp_path):
    app = _create_app(tmp_path)
    response = app.test_client().get("/auth/password")
    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_password_change_via_api_and_mcp_parity(tmp_path):
    token = "obk_maria-token"
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True, API_AUTH_TOKEN="global")
    _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A", token=token)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {token}"}

    wrong = client.post(
        "/api/v1/users/me/password",
        headers=headers,
        json={"current_password": "falsch", "new_password": "neuesgeheim"},
    )
    assert wrong.status_code == 403
    short = client.post(
        "/api/v1/users/me/password",
        headers=headers,
        json={"current_password": "geheim123", "new_password": "abc"},
    )
    assert short.status_code == 400
    no_user = client.post(
        "/api/v1/users/me/password",
        headers={"Authorization": "Bearer global"},
        json={"current_password": "geheim123", "new_password": "neuesgeheim"},
    )
    assert no_user.status_code == 400

    changed = client.post(
        "/api/v1/users/me/password",
        headers=headers,
        json={"current_password": "geheim123", "new_password": "neuesgeheim"},
    )
    assert changed.status_code == 200
    ui = app.test_client()
    assert "Login erfolgreich" in _login(
        ui, username="maria", password="neuesgeheim", follow_redirects=True
    ).get_data(as_text=True)

    # MCP-Tools bilden dieselben Endpunkte ab.
    http = _RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "change_own_password",
                       "arguments": {"current_password": "a", "new_password": "b"}},
        }
    )
    assert http.calls[-1] == (
        "POST", "/users/me/password", None, {"current_password": "a", "new_password": "b"}
    )
    server.handle(
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "set_user_password",
                       "arguments": {"user_id": 5, "new_password": "b"}},
        }
    )
    assert http.calls[-1] == ("POST", "/users/5/password", None, {"new_password": "b"})
    server.handle(
        {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "unlock_user_login", "arguments": {"user_id": 5}},
        }
    )
    assert http.calls[-1] == ("POST", "/users/5/unlock", None, {})


def test_admin_sets_user_password_via_ui(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    user_id = _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()
    _login(client)

    short = client.post(
        f"/users/{user_id}/password", data={"new_password": "abc"}, follow_redirects=True
    )
    assert "mindestens 8 Zeichen".encode() in short.data
    ok = client.post(
        f"/users/{user_id}/password", data={"new_password": "neuesgeheim"},
        follow_redirects=True,
    )
    assert "Passwort wurde gesetzt".encode() in ok.data
    fresh = app.test_client()
    assert "Login erfolgreich" in _login(
        fresh, username="maria", password="neuesgeheim", follow_redirects=True
    ).get_data(as_text=True)


def test_generate_api_token_lookup_still_works_after_constant_time_change(tmp_path):
    token = generate_api_token()
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, token=token)
    response = app.test_client().get(
        "/api/v1/companies", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
