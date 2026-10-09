"""KI-Zugang (LLM-API-Key) je Benutzer: Pflege über UI/API/MCP und Verwendung."""

import base64
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services import receipt_ocr
from app.services.chat import chat_tool_blocked
from app.services.llm_settings import (
    OPENAI_RESPONSES_URL,
    PURPOSE_CHAT,
    PURPOSE_RECEIPT_OCR,
    LlmSettingsError,
    decrypt_api_key,
    encrypt_api_key,
    normalize_endpoint_url,
    resolve_llm_endpoint,
)
from app.services.mcp_server import ApiResponse, MCPServer
from domain.models import AuditLog, Company, Tenant, User

API_KEY = "sk-test-0123456789abcdef"


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'llm.db'}",
    }
    config.update(overrides)
    return create_app(config)


def _seed_user(
    app,
    *,
    username: str = "admin",
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
            password_hash=hash_password("geheim123"),
            role=role,
            tenant_id=tenant_id,
            api_token_hash=hash_api_token(token) if token else None,
            api_token_last4=token[-4:] if token else None,
        )
        session.add(user)
        session.commit()
        return user.id


def _login(client, username: str = "admin"):
    return client.post("/auth/login", data={"username": username, "password": "geheim123"})


def _load_user(app, user_id: int) -> User:
    with app.extensions["db_session_factory"]() as session:
        user = session.get(User, user_id)
        session.expunge(user)
        return user


# ---------------------------------------------------------------------------
# Verschlüsselung und Validierung
# ---------------------------------------------------------------------------


def test_api_key_encryption_roundtrip_and_wrong_secret():
    token = encrypt_api_key(API_KEY, secret_key="secret-a")
    assert API_KEY not in token
    assert decrypt_api_key(token, secret_key="secret-a") == API_KEY
    # Nach einem SECRET_KEY-Wechsel ist der Key nicht mehr lesbar.
    assert decrypt_api_key(token, secret_key="secret-b") is None
    assert decrypt_api_key(None, secret_key="secret-a") is None


def test_normalize_endpoint_url():
    assert (
        normalize_endpoint_url("https://openrouter.ai/api/v1")
        == "https://openrouter.ai/api/v1/responses"
    )
    assert (
        normalize_endpoint_url(" http://localhost:11434/v1/responses ")
        == "http://localhost:11434/v1/responses"
    )
    for invalid in ("", "ftp://example.com/v1", "example.com/v1/responses"):
        with pytest.raises(LlmSettingsError):
            normalize_endpoint_url(invalid)
    with pytest.raises(LlmSettingsError, match="Zugangsdaten"):
        normalize_endpoint_url("https://user:pw@example.com/v1/responses")


# ---------------------------------------------------------------------------
# REST-API
# ---------------------------------------------------------------------------


def test_api_sets_openai_key_without_exposing_it(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app, username="maria", role="Buchhalter", tenant_name="Mandant A")
    client = app.test_client()

    response = client.post(f"/api/v1/users/{user_id}/llm", json={"api_key": API_KEY})
    assert response.status_code == 200
    body = response.get_json()
    assert body["llm"] == {
        "configured": True,
        "provider": "openai",
        "provider_label": "OpenAI",
        "endpoint_url": OPENAI_RESPONSES_URL,
        "model": "gpt-4.1-mini",
        "api_key_last4": "cdef",
        "api_key_readable": True,
    }
    assert API_KEY not in response.get_data(as_text=True)

    user = _load_user(app, user_id)
    assert user.llm_provider == "openai"
    assert API_KEY not in user.llm_api_key_encrypted
    assert decrypt_api_key(user.llm_api_key_encrypted, secret_key="testing-secret-key") == API_KEY

    listed = client.get("/api/v1/users").get_data(as_text=True)
    assert API_KEY not in listed and "cdef" in listed

    # Audit-Eintrag ohne Key.
    with app.extensions["db_session_factory"]() as session:
        event = session.execute(
            select(AuditLog).where(AuditLog.action == "llm_settings_updated")
        ).scalar_one()
        assert API_KEY not in json.dumps(event.payload)
        assert event.payload["api_key_changed"] is True


def test_api_validates_and_keeps_existing_key(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app, username="maria", role="Buchhalter")
    client = app.test_client()

    missing_key = client.post(f"/api/v1/users/{user_id}/llm", json={"provider": "openai"})
    assert missing_key.status_code == 400
    assert "API-Key" in missing_key.get_json()["error"]

    missing_url = client.post(f"/api/v1/users/{user_id}/llm", json={"provider": "custom"})
    assert missing_url.status_code == 400

    bad_provider = client.post(f"/api/v1/users/{user_id}/llm", json={"provider": "foo"})
    assert bad_provider.status_code == 400

    bad_type = client.post(f"/api/v1/users/{user_id}/llm", json={"api_key": 123})
    assert bad_type.status_code == 400

    # Eigener Endpunkt ohne Key (z. B. lokales Ollama) ist erlaubt.
    custom = client.post(
        f"/api/v1/users/{user_id}/llm",
        json={
            "provider": "custom",
            "endpoint_url": "http://localhost:11434/v1",
            "model": "llama3.1",
        },
    )
    assert custom.status_code == 200
    assert custom.get_json()["llm"]["endpoint_url"] == "http://localhost:11434/v1/responses"
    assert custom.get_json()["llm"]["api_key_last4"] is None

    # Key setzen, dann Modell ändern ohne Key: der Key bleibt erhalten.
    client.post(
        f"/api/v1/users/{user_id}/llm",
        json={"provider": "custom", "endpoint_url": "https://llm.example/v1", "api_key": API_KEY},
    )
    switched = client.post(
        f"/api/v1/users/{user_id}/llm", json={"provider": "openai", "model": "gpt-5-mini"}
    )
    assert switched.status_code == 200
    assert switched.get_json()["llm"]["api_key_last4"] == "cdef"
    assert switched.get_json()["llm"]["model"] == "gpt-5-mini"
    assert _load_user(app, user_id).llm_endpoint_url is None

    cleared = client.post(f"/api/v1/users/{user_id}/llm/delete")
    assert cleared.status_code == 200
    assert cleared.get_json()["llm"] == {"configured": False}
    user = _load_user(app, user_id)
    assert user.llm_api_key_encrypted is None and user.llm_provider is None


def test_api_llm_settings_require_admin_and_scope(tmp_path):
    app = _create_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_user(app, username="buchhalter", role="Buchhalter", tenant_name="A", token="obk_b")
    _seed_user(app, username="tenantadmin", role="Admin", tenant_name="A", token="obk_ta")
    foreign_id = _seed_user(app, username="fremd", role="Buchhalter", tenant_name="B")
    own_id = _seed_user(app, username="kollege", role="Buchhalter", tenant_name="A")
    client = app.test_client()

    denied = client.post(
        f"/api/v1/users/{own_id}/llm",
        json={"api_key": API_KEY},
        headers={"Authorization": "Bearer obk_b"},
    )
    assert denied.status_code == 403

    foreign = client.post(
        f"/api/v1/users/{foreign_id}/llm",
        json={"api_key": API_KEY},
        headers={"Authorization": "Bearer obk_ta"},
    )
    assert foreign.status_code == 404

    own = client.post(
        f"/api/v1/users/{own_id}/llm",
        json={"api_key": API_KEY},
        headers={"Authorization": "Bearer obk_ta"},
    )
    assert own.status_code == 200


# ---------------------------------------------------------------------------
# Verwaltungsoberfläche
# ---------------------------------------------------------------------------


def test_admin_ui_sets_and_clears_llm_access(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app)
    user_id = _seed_user(app, username="maria", role="Buchhalter")
    client = app.test_client()
    _login(client)

    page = client.get("/verwaltung").get_data(as_text=True)
    assert "KI-Zugang (LLM) je Benutzer" in page
    assert "Instanz-Standard" in page

    invalid = client.post(
        "/users/llm",
        data={"user_id": user_id, "provider": "custom", "endpoint_url": "nope"},
        follow_redirects=True,
    )
    assert "http:// oder https://" in invalid.get_data(as_text=True)

    saved = client.post(
        "/users/llm",
        data={"user_id": user_id, "provider": "openai", "model": "", "api_key": API_KEY},
        follow_redirects=True,
    )
    text = saved.get_data(as_text=True)
    assert "KI-Zugang für maria wurde gespeichert" in text
    assert "...cdef" in text
    assert API_KEY not in text

    cleared = client.post(f"/users/{user_id}/llm/delete", follow_redirects=True)
    assert "KI-Zugang wurde entfernt" in cleared.get_data(as_text=True)
    assert _load_user(app, user_id).llm_provider is None


def test_non_admin_cannot_set_llm_access_via_ui(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, username="buchhalter", role="Buchhalter")
    user_id = _seed_user(app, username="maria", role="Buchhalter")
    client = app.test_client()
    _login(client, "buchhalter")

    response = client.post(
        "/users/llm", data={"user_id": user_id, "api_key": API_KEY}, follow_redirects=True
    )
    assert "nur ein Administrator" in response.get_data(as_text=True)
    assert _load_user(app, user_id).llm_provider is None


# ---------------------------------------------------------------------------
# MCP und Chat-Sperre
# ---------------------------------------------------------------------------


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        self.calls.append((method, path, params, json_body))
        return ApiResponse(status=200, text="{}", content_type="application/json", json={})


def test_mcp_tools_map_to_llm_endpoints():
    http = _RecordingHttp()
    server = MCPServer(http=http)
    server.handle(
        {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "set_user_llm_settings",
                "arguments": {"user_id": 5, "provider": "openai", "api_key": "sk-x"},
            },
        }
    )
    assert http.calls[-1] == (
        "POST", "/users/5/llm", None, {"provider": "openai", "api_key": "sk-x"}
    )
    server.handle(
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "clear_user_llm_settings", "arguments": {"user_id": 5}},
        }
    )
    assert http.calls[-1] == ("POST", "/users/5/llm/delete", None, {})


def test_llm_settings_tools_are_blocked_in_chat():
    assert chat_tool_blocked("set_user_llm_settings")
    assert chat_tool_blocked("clear_user_llm_settings")


# ---------------------------------------------------------------------------
# Verwendung: Benutzer-Zugang vor Instanz-Endpoint
# ---------------------------------------------------------------------------


def test_resolve_prefers_user_access_over_instance(tmp_path):
    app = _create_app(
        tmp_path,
        CHAT_LLM_ENDPOINT_URL="http://instance.test/v1/responses",
        CHAT_LLM_MODEL="instance-model",
    )
    with_key = _seed_user(app, username="maria", role="Buchhalter")
    without_key = _seed_user(app, username="paul", role="Buchhalter")
    client = app.test_client()
    client.post(f"/api/v1/users/{with_key}/llm", json={"api_key": API_KEY})

    with app.app_context():
        user_endpoint = resolve_llm_endpoint(PURPOSE_CHAT, user_id=with_key)
        assert user_endpoint.url == OPENAI_RESPONSES_URL
        assert user_endpoint.api_key == API_KEY
        assert user_endpoint.model == "gpt-4.1-mini"
        assert user_endpoint.source == "user"

        instance_endpoint = resolve_llm_endpoint(PURPOSE_CHAT, user_id=without_key)
        assert instance_endpoint.url == "http://instance.test/v1/responses"
        assert instance_endpoint.model == "instance-model"
        assert instance_endpoint.api_key is None

        # OCR hat keinen Instanz-Endpoint, der Benutzer-Zugang greift trotzdem.
        assert resolve_llm_endpoint(PURPOSE_RECEIPT_OCR, user_id=without_key) is None
        assert resolve_llm_endpoint(PURPOSE_RECEIPT_OCR, user_id=with_key).api_key == API_KEY


def test_chat_uses_users_api_key(tmp_path):
    app = _create_app(tmp_path)
    _seed_user(app, tenant_name="Mandant A")
    with app.extensions["db_session_factory"]() as session:
        tenant = session.execute(select(Tenant)).scalar_one()
        company = Company(name="Chat GmbH", currency_code="EUR", tenant_id=tenant.id)
        session.add(company)
        session.commit()
        company_id = company.id
    client = app.test_client()
    _login(client)

    # Ohne KI-Zugang und ohne Instanz-Endpoint: verständliche Fehlermeldung.
    missing = client.post("/chat/send", data={"company_id": company_id, "message": "Hallo"})
    assert missing.status_code == 400
    assert "KI-Zugang" in missing.get_json()["error"]

    with app.extensions["db_session_factory"]() as session:
        admin_id = session.execute(select(User.id).where(User.username == "admin")).scalar_one()
    client.post(
        "/users/llm",
        data={
            "user_id": admin_id,
            "provider": "custom",
            "endpoint_url": "https://llm.example/v1",
            "model": "my-model",
            "api_key": API_KEY,
        },
    )

    captured = {}

    def poster(endpoint_url, payload, *, api_key=None, timeout=None):
        captured.update(url=endpoint_url, api_key=api_key, model=payload["model"])
        return {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Hallo zurück"}],
                }
            ]
        }

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send", data={"company_id": company_id, "message": "Hallo"}
        )
    assert response.status_code == 200, response.get_data(as_text=True)
    assert captured == {
        "url": "https://llm.example/v1/responses",
        "api_key": API_KEY,
        "model": "my-model",
    }


# ---------------------------------------------------------------------------
# Beleg-OCR: Bearer-Header und PDF als input_file
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_ocr_sends_bearer_key_and_pdf_as_input_file(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["auth"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data)
        return _FakeResponse({"output_text": "Gesamtbetrag 119,00 EUR"})

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    text = receipt_ocr._ocr_via_endpoint(
        endpoint_url=OPENAI_RESPONSES_URL,
        model="gpt-4.1-mini",
        file_bytes=b"%PDF-1.4 scan",
        mime_type="application/pdf",
        file_name="scan.pdf",
        api_key=API_KEY,
    )
    assert "119,00" in text
    assert captured["auth"] == f"Bearer {API_KEY}"
    block = captured["payload"]["input"][1]["content"][0]
    assert block["type"] == "input_file"
    assert block["filename"] == "scan.pdf"
    assert block["file_data"] == (
        "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 scan").decode()
    )


def test_ocr_without_key_sends_no_authorization(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["auth"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data)
        return _FakeResponse({"output_text": "Text"})

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    receipt_ocr._ocr_via_endpoint(
        endpoint_url="http://localhost/v1/responses",
        model="m",
        file_bytes=b"\x89PNG",
        mime_type="image/png",
        file_name="scan.png",
    )
    assert captured["auth"] is None
    assert captured["payload"]["input"][1]["content"][0]["type"] == "input_image"
