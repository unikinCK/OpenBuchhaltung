"""OAuth 2.1 für MCP-Connectoren: Discovery, DCR, PKCE-Flow, Refresh, Widerruf, MCP."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import select

from app import create_app
from app.auth import hash_password
from app.services.mcp_http import make_server
from app.services.mcp_server import ApiResponse, MCPServer, current_api_token_override
from domain.models import OAuthGrant, Tenant, User

BASE = "https://obh.test"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
GLOBAL_TOKEN = "global-backend-token"
VERIFIER = "v" * 43 + "-pkce-verifier"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'oauth.db'}",
        "API_REQUIRE_AUTH": True,
        "API_AUTH_TOKEN": GLOBAL_TOKEN,
    }
    config.update(overrides)
    return create_app(config)


def _seed_user(app, username: str = "maria", role: str = "Buchhalter", tenant: bool = True):
    with app.extensions["db_session_factory"]() as session:
        tenant_id = None
        if tenant:
            tenant_row = Tenant(name=f"Mandant {username}")
            session.add(tenant_row)
            session.flush()
            tenant_id = tenant_row.id
        user = User(
            username=username,
            password_hash=hash_password("geheim123"),
            role=role,
            tenant_id=tenant_id,
        )
        session.add(user)
        session.commit()
        return user.id


def _client(app):
    return app.test_client()


def _login(client, username: str = "maria"):
    response = client.post(
        "/auth/login", data={"username": username, "password": "geheim123"}, base_url=BASE
    )
    assert response.status_code == 302


def _register(client, **overrides) -> dict:
    payload = {
        "client_name": "ChatGPT",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
    }
    payload.update(overrides)
    response = client.post("/oauth/register", json=payload, base_url=BASE)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _authorize_params(client_id: str, **overrides) -> dict:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "state": "xyz",
        "scope": "mcp",
        "code_challenge": _challenge(VERIFIER),
        "code_challenge_method": "S256",
        "resource": f"{BASE}/mcp",
    }
    params.update(overrides)
    return params


def _obtain_code(client, client_id: str, decision: str = "allow") -> dict:
    response = client.post(
        "/oauth/authorize",
        data={**_authorize_params(client_id), "decision": decision},
        base_url=BASE,
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith(REDIRECT + "?")
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}


def _exchange(client, client_id: str, code: str, verifier: str = VERIFIER, **extra):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            **extra,
        },
        base_url=BASE,
    )


def _tokens(app, username: str = "maria") -> tuple[dict, dict]:
    client = _client(app)
    _login(client, username)
    registered = _register(client)
    code = _obtain_code(client, registered["client_id"])["code"]
    response = _exchange(client, registered["client_id"], code)
    assert response.status_code == 200, response.get_json()
    return registered, response.get_json()


def _whoami(app, token: str):
    return _client(app).get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})


# --- Discovery -----------------------------------------------------------------------


def test_discovery_metadata(tmp_path):
    client = _client(_create_app(tmp_path))
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        resource = client.get(path, base_url=BASE).get_json()
        assert resource["resource"] == f"{BASE}/mcp"
        assert resource["authorization_servers"] == [BASE]

    server = client.get("/.well-known/oauth-authorization-server", base_url=BASE).get_json()
    assert server["issuer"] == BASE
    assert server["authorization_endpoint"] == f"{BASE}/oauth/authorize"
    assert server["token_endpoint"] == f"{BASE}/oauth/token"
    assert server["registration_endpoint"] == f"{BASE}/oauth/register"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert "none" in server["token_endpoint_auth_methods_supported"]


def test_oauth_can_be_disabled(tmp_path):
    app = _create_app(tmp_path, OAUTH_ENABLED=False)
    client = _client(app)
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404
    assert client.post("/oauth/register", json={"redirect_uris": [REDIRECT]}).status_code == 404


# --- Dynamic Client Registration ------------------------------------------------------


def test_register_validates_redirect_uris(tmp_path):
    client = _client(_create_app(tmp_path))
    bad = client.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]})
    assert bad.status_code == 400
    assert bad.get_json()["error"] == "invalid_redirect_uri"
    assert client.post("/oauth/register", json={}).status_code == 400

    loopback = _register(client, redirect_uris=["http://127.0.0.1:33418/callback"])
    assert loopback["client_id"].startswith("obc_")
    assert "client_secret" not in loopback


def test_register_respects_allowed_redirect_hosts(tmp_path):
    app = _create_app(tmp_path, OAUTH_ALLOWED_REDIRECT_HOSTS="chatgpt.com, claude.ai")
    client = _client(app)
    _register(client)
    other = client.post("/oauth/register", json={"redirect_uris": ["https://evil.example/cb"]})
    assert other.status_code == 400


# --- Autorisierung --------------------------------------------------------------------


def test_authorize_requires_login_and_shows_consent(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = _client(app)
    registered = _register(client)
    params = _authorize_params(registered["client_id"])

    anonymous = client.get("/oauth/authorize", query_string=params, base_url=BASE)
    assert anonymous.status_code == 302
    login_location = anonymous.headers["Location"]
    assert "/auth/login" in login_location
    next_path = parse_qs(urlsplit(login_location).query)["next"][0]
    assert next_path.startswith("/oauth/authorize?") and "code_challenge=" in next_path

    _login(client)
    consent = client.get("/oauth/authorize", query_string=params, base_url=BASE)
    assert consent.status_code == 200
    page = consent.get_data(as_text=True)
    assert "ChatGPT" in page and "maria" in page and "chatgpt.com" in page
    assert "form-action 'self' https://chatgpt.com" in consent.headers["Content-Security-Policy"]


def test_authorize_rejects_unknown_client_and_missing_pkce(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = _client(app)
    _login(client)
    unknown = client.get(
        "/oauth/authorize", query_string=_authorize_params("obc_unbekannt"), base_url=BASE
    )
    assert unknown.status_code == 400

    registered = _register(client)
    unregistered_redirect = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(
            registered["client_id"], redirect_uri="https://evil.example/cb"
        ),
        base_url=BASE,
    )
    assert unregistered_redirect.status_code == 400  # kein Redirect an fremde URIs

    no_pkce = client.get(
        "/oauth/authorize",
        query_string=_authorize_params(registered["client_id"], code_challenge=""),
        base_url=BASE,
    )
    assert no_pkce.status_code == 302
    assert "error=invalid_request" in no_pkce.headers["Location"]


def test_deny_returns_access_denied(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = _client(app)
    _login(client)
    registered = _register(client)
    result = _obtain_code(client, registered["client_id"], decision="deny")
    assert result == {"error": "access_denied", "state": "xyz"}


# --- Token-Endpunkt -------------------------------------------------------------------


def test_full_flow_issues_tokens_that_act_as_user(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    _registered, tokens = _tokens(app)

    assert tokens["token_type"] == "Bearer"
    assert tokens["access_token"].startswith("obo_")
    assert tokens["refresh_token"].startswith("obr_")
    assert tokens["expires_in"] == 3600

    me = _whoami(app, tokens["access_token"])
    assert me.status_code == 200
    assert me.get_json()["user"]["id"] == user_id
    assert me.get_json()["global_access"] is False
    assert _whoami(app, tokens["refresh_token"]).status_code == 401


def test_code_is_single_use_and_pkce_checked(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = _client(app)
    _login(client)
    registered = _register(client)
    code = _obtain_code(client, registered["client_id"])["code"]

    wrong = _exchange(client, registered["client_id"], code, verifier="x" * 43)
    assert wrong.status_code == 400 and wrong.get_json()["error"] == "invalid_grant"
    assert _exchange(client, registered["client_id"], code).status_code == 200
    reused = _exchange(client, registered["client_id"], code)
    assert reused.status_code == 400 and reused.get_json()["error"] == "invalid_grant"

    other = _register(client, client_name="Andere App")
    code2 = _obtain_code(client, registered["client_id"])["code"]
    assert _exchange(client, other["client_id"], code2).status_code == 400


def test_refresh_rotates_tokens(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    registered, tokens = _tokens(app)
    client = _client(app)

    refreshed = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
        },
        base_url=BASE,
    )
    assert refreshed.status_code == 200
    new = refreshed.get_json()
    assert new["access_token"] != tokens["access_token"]
    assert _whoami(app, new["access_token"]).status_code == 200
    assert _whoami(app, tokens["access_token"]).status_code == 401

    replay = client.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "client_id": registered["client_id"],
        },
        base_url=BASE,
    )
    assert replay.status_code == 400


def test_confidential_client_needs_secret(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    client = _client(app)
    _login(client)
    registered = _register(client, token_endpoint_auth_method="client_secret_post")
    assert registered["client_secret"]
    code = _obtain_code(client, registered["client_id"])["code"]

    missing = _exchange(client, registered["client_id"], code)
    assert missing.status_code == 401 and missing.get_json()["error"] == "invalid_client"
    ok = _exchange(client, registered["client_id"], code, client_secret=registered["client_secret"])
    assert ok.status_code == 200


def test_expired_access_token_and_inactive_user_are_rejected(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app)
    _registered, tokens = _tokens(app)
    factory = app.extensions["db_session_factory"]

    with factory() as session:
        grant = session.execute(select(OAuthGrant)).scalar_one()
        grant.access_token_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()
    assert _whoami(app, tokens["access_token"]).status_code == 401

    with factory() as session:
        grant = session.execute(select(OAuthGrant)).scalar_one()
        grant.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        session.get(User, user_id).is_active = False
        session.commit()
    assert _whoami(app, tokens["access_token"]).status_code == 401


def test_revocation_endpoint(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    registered, tokens = _tokens(app)
    response = _client(app).post(
        "/oauth/revoke",
        data={"token": tokens["refresh_token"], "client_id": registered["client_id"]},
        base_url=BASE,
    )
    assert response.status_code == 200
    assert _whoami(app, tokens["access_token"]).status_code == 401


# --- Verbundene Apps: UI, API, MCP-Parität ---------------------------------------------


def test_connected_apps_page_lists_and_revokes(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    _registered, tokens = _tokens(app)
    client = _client(app)
    _login(client)

    page = client.get("/auth/apps", base_url=BASE)
    assert page.status_code == 200
    assert "ChatGPT" in page.get_data(as_text=True)

    with app.extensions["db_session_factory"]() as session:
        grant_id = session.execute(select(OAuthGrant.id)).scalar_one()
    revoked = client.post(f"/auth/apps/{grant_id}/revoke", base_url=BASE)
    assert revoked.status_code == 302
    assert _whoami(app, tokens["access_token"]).status_code == 401
    after = client.get("/auth/apps", base_url=BASE).get_data(as_text=True)
    assert "Keine aktiven Verbindungen" in after


def test_grants_api_scopes_by_user(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, "maria")
    _seed_user(app, "otto")
    _maria_client, maria_tokens = _tokens(app, "maria")
    _otto_client, otto_tokens = _tokens(app, "otto")
    client = _client(app)
    maria = {"Authorization": f"Bearer {maria_tokens['access_token']}"}
    admin = {"Authorization": f"Bearer {GLOBAL_TOKEN}"}

    own = client.get("/api/v1/oauth/grants", headers=maria).get_json()
    assert [grant["username"] for grant in own] == ["maria"]
    everything = client.get("/api/v1/oauth/grants", headers=admin).get_json()
    assert sorted(grant["username"] for grant in everything) == ["maria", "otto"]

    otto_grant = next(grant for grant in everything if grant["username"] == "otto")
    forbidden = client.post(f"/api/v1/oauth/grants/{otto_grant['id']}/revoke", headers=maria)
    assert forbidden.status_code == 404
    revoked = client.post(f"/api/v1/oauth/grants/{otto_grant['id']}/revoke", headers=admin)
    assert revoked.status_code == 200 and revoked.get_json()["active"] is False
    assert _whoami(app, otto_tokens["access_token"]).status_code == 401
    assert _whoami(app, maria_tokens["access_token"]).status_code == 200

    history = client.get(
        "/api/v1/oauth/grants", headers=admin, query_string={"include_inactive": "true"}
    ).get_json()
    assert len(history) == 2


# --- MCP ------------------------------------------------------------------------------


class FlaskHttp:
    def __init__(self, client, token: str | None) -> None:
        self.client = client
        self.token = token
        self._lock = threading.Lock()

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        token = current_api_token_override() or self.token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with self._lock:
            response = self.client.open(
                f"/api/v1{path}", method=method, query_string=params, json=json_body,
                headers=headers,
            )
        return ApiResponse(
            status=response.status_code,
            text=response.get_data(as_text=True),
            content_type=response.content_type or "",
            json=response.get_json(silent=True),
        )


@pytest.fixture
def mcp_url(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    httpd = make_server(
        MCPServer(http=FlaskHttp(app.test_client(), GLOBAL_TOKEN)),
        host="127.0.0.1",
        port=0,
        path="/mcp",
        auth_token="mcp-secret",
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    httpd.server_close()


def _mcp_post(url: str, headers: dict[str, str]):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_current_user", "arguments": {}},
    }
    request = Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    return urlopen(request, timeout=5)


def test_mcp_401_points_to_oauth_metadata(mcp_url):
    _app, url = mcp_url
    with pytest.raises(HTTPError) as excinfo:
        _mcp_post(url, {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "obh.test"})
    assert excinfo.value.code == 401
    assert excinfo.value.headers["WWW-Authenticate"] == (
        'Bearer resource_metadata="https://obh.test/.well-known/oauth-protected-resource/mcp"'
    )

    with pytest.raises(HTTPError) as excinfo:
        _mcp_post(url, {"Authorization": "Bearer obo_abgelaufen"})
    assert 'error="invalid_token"' in excinfo.value.headers["WWW-Authenticate"]


def test_mcp_accepts_oauth_access_token(mcp_url):
    app, url = mcp_url
    _registered, tokens = _tokens(app)
    with _mcp_post(url, {"Authorization": f"Bearer {tokens['access_token']}"}) as response:
        body = json.loads(response.read())
    identity = json.loads(body["result"]["content"][0]["text"])
    assert identity["auth"] == "user"
    assert identity["user"]["username"] == "maria"
