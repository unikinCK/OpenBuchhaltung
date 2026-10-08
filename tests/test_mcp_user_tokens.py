"""Benutzer-API-Tokens am MCP-HTTP-Endpunkt und ``GET /api/v1/users/me``."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services.mcp_http import UserTokenValidator, make_server, resolve_authorization
from app.services.mcp_server import (
    ApiResponse,
    MCPServer,
    api_token_override,
    current_api_token_override,
)
from domain.models import Tenant, User

GLOBAL_TOKEN = "global-backend-token"
MARIA_TOKEN = "obk_maria-token"
MCP_SECRET = "mcp-secret"


def _create_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'mcp_tokens.db'}",
            "API_REQUIRE_AUTH": True,
            "API_AUTH_TOKEN": GLOBAL_TOKEN,
        }
    )


def _seed_maria(app) -> tuple[int, int]:
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Mandant A")
        session.add(tenant)
        session.flush()
        user = User(
            username="maria",
            password_hash=hash_password("geheim123"),
            role="Buchhalter",
            tenant_id=tenant.id,
            api_token_hash=hash_api_token(MARIA_TOKEN),
            api_token_last4=MARIA_TOKEN[-4:],
        )
        session.add(user)
        session.commit()
        return user.id, tenant.id


class FlaskHttp:
    """MCP-Backend-Client gegen den Flask-Testclient (mit Token-Override wie HttpApiClient)."""

    def __init__(self, client, token: str | None) -> None:
        self.client = client
        self.token = token
        self.calls: list[tuple[str, str, str | None]] = []
        self._lock = threading.Lock()

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        token = current_api_token_override() or self.token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with self._lock:
            self.calls.append((method, path, token))
            response = self.client.open(
                f"/api/v1{path}",
                method=method,
                query_string=params,
                json=json_body,
                headers=headers,
            )
        return ApiResponse(
            status=response.status_code,
            text=response.get_data(as_text=True),
            content_type=response.content_type or "",
            json=response.get_json(silent=True),
        )


def test_users_me_reports_identity(tmp_path):
    app = _create_app(tmp_path)
    user_id, tenant_id = _seed_maria(app)
    client = app.test_client()

    as_user = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {MARIA_TOKEN}"})
    assert as_user.status_code == 200
    assert as_user.get_json() == {
        "auth": "user",
        "user": {"id": user_id, "username": "maria", "role": "Buchhalter", "tenant_id": tenant_id},
        "global_access": False,
    }

    as_global = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"})
    assert as_global.status_code == 200
    assert as_global.get_json() == {"auth": "api_token", "user": None, "global_access": True}

    assert client.get("/api/v1/users/me").status_code == 401
    assert (
        client.get("/api/v1/users/me", headers={"Authorization": "Bearer falsch"}).status_code
        == 401
    )


def test_api_token_override_is_scoped():
    assert current_api_token_override() is None
    with api_token_override("abc"):
        assert current_api_token_override() == "abc"
        with api_token_override(None):
            assert current_api_token_override() is None
        assert current_api_token_override() == "abc"
    assert current_api_token_override() is None


def test_resolve_authorization(tmp_path):
    app = _create_app(tmp_path)
    _seed_maria(app)
    http = FlaskHttp(app.test_client(), GLOBAL_TOKEN)
    validator = UserTokenValidator(MCPServer(http=http))

    assert resolve_authorization(f"Bearer {MCP_SECRET}", MCP_SECRET, validator) == (True, None)
    assert resolve_authorization(f"Bearer {MARIA_TOKEN}", MCP_SECRET, validator) == (
        True,
        MARIA_TOKEN,
    )
    assert resolve_authorization("Bearer falsch", MCP_SECRET, validator) == (False, None)
    assert resolve_authorization(None, MCP_SECRET, validator) == (False, None)
    # Ohne Benutzer-Token-Unterstützung zählt nur der Eingangstoken.
    assert resolve_authorization(f"Bearer {MARIA_TOKEN}", MCP_SECRET, None) == (False, None)
    # Ohne Eingangstoken (Loopback) bleibt der Endpunkt offen; gültige Benutzer-Tokens
    # werden trotzdem durchgereicht.
    assert resolve_authorization(None, None, validator) == (True, None)
    assert resolve_authorization(f"Bearer {MARIA_TOKEN}", None, validator) == (True, MARIA_TOKEN)


def test_user_token_validator_caches_positive_results(tmp_path):
    app = _create_app(tmp_path)
    _seed_maria(app)
    http = FlaskHttp(app.test_client(), GLOBAL_TOKEN)
    validator = UserTokenValidator(MCPServer(http=http))

    assert validator.is_valid(MARIA_TOKEN) is True
    assert validator.is_valid(MARIA_TOKEN) is True
    assert validator.is_valid("falsch") is False
    assert validator.is_valid("falsch") is False
    me_calls = [call for call in http.calls if call[1] == "/users/me"]
    assert me_calls == [
        ("GET", "/users/me", MARIA_TOKEN),
        ("GET", "/users/me", "falsch"),
        ("GET", "/users/me", "falsch"),
    ]


def _post(url: str, token: str | None, message: dict) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=json.dumps(message).encode(), headers=headers, method="POST")
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def _whoami(url: str, token: str | None) -> dict:
    body = _post(
        url,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_current_user", "arguments": {}},
        },
    )
    return json.loads(body["result"]["content"][0]["text"])


@pytest.fixture
def mcp_endpoint(tmp_path):
    app = _create_app(tmp_path)
    _seed_maria(app)
    http = FlaskHttp(app.test_client(), GLOBAL_TOKEN)
    servers = []

    def start(allow_user_tokens: bool = True) -> str:
        httpd = make_server(
            MCPServer(http=http),
            host="127.0.0.1",
            port=0,
            path="/mcp",
            auth_token=MCP_SECRET,
            allow_user_tokens=allow_user_tokens,
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/mcp"

    yield start, http
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_mcp_http_forwards_user_token(mcp_endpoint):
    start, http = mcp_endpoint
    url = start()

    as_maria = _whoami(url, MARIA_TOKEN)
    assert as_maria["auth"] == "user"
    assert as_maria["user"]["username"] == "maria"
    assert as_maria["global_access"] is False
    # Der Tool-Aufruf lief mit Marias Token, nicht mit dem Server-Token.
    assert http.calls[-1] == ("GET", "/users/me", MARIA_TOKEN)

    as_server = _whoami(url, MCP_SECRET)
    assert as_server == {"auth": "api_token", "user": None, "global_access": True}
    assert http.calls[-1] == ("GET", "/users/me", GLOBAL_TOKEN)

    for token in ("falsch", None):
        with pytest.raises(HTTPError) as excinfo:
            _whoami(url, token)
        assert excinfo.value.code == 401


def test_mcp_http_user_tokens_can_be_disabled(mcp_endpoint):
    start, _http = mcp_endpoint
    url = start(allow_user_tokens=False)

    with pytest.raises(HTTPError) as excinfo:
        _whoami(url, MARIA_TOKEN)
    assert excinfo.value.code == 401
    assert _whoami(url, MCP_SECRET)["auth"] == "api_token"
