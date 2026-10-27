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

    def poster(endpoint_url, payload, *, api_key=None, timeout=None, on_event=None):
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


def _fake_server(routes, seen):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout=None):
        seen.append((request.full_url, request.get_header("Authorization")))
        if request.full_url in routes:
            return _FakeResponse(routes[request.full_url])
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    return fake_urlopen


def test_loaded_model_uses_model_loaded_in_lmstudio(tmp_path):
    app = _create_app(
        tmp_path,
        CHAT_LLM_ENDPOINT_URL="http://studio.test:1234/v1/responses",
        CHAT_LLM_MODEL="loaded",
    )
    routes = {
        "http://studio.test:1234/api/v1/models": {
            "models": [
                {"type": "embedding", "key": "embed", "loaded_instances": [{"id": "embed"}]},
                {"type": "llm", "key": "other", "loaded_instances": []},
                {"type": "llm", "key": "vision", "loaded_instances": [{"id": "vision"}]},
            ]
        }
    }
    seen: list = []
    with app.app_context(), patch(
        "app.services.llm_settings.urlopen", _fake_server(routes, seen)
    ):
        endpoint = resolve_llm_endpoint(PURPOSE_CHAT)
    assert endpoint.model == "vision"
    assert endpoint.url == "http://studio.test:1234/v1/responses"
    assert [url for url, _ in seen] == ["http://studio.test:1234/api/v1/models"]


def test_loaded_model_falls_back_to_lmstudio_v0_and_ollama():
    from app.services.llm_settings import find_loaded_model

    seen: list = []
    v0 = {
        "http://lm.test/api/v0/models": {
            "data": [
                {"id": "idle", "state": "not-loaded", "type": "llm"},
                {"id": "active", "state": "loaded", "type": "vlm"},
            ]
        }
    }
    with patch("app.services.llm_settings.urlopen", _fake_server(v0, seen)):
        assert find_loaded_model("http://lm.test/v1/responses", api_key="k") == "active"
    assert seen[0] == ("http://lm.test/api/v1/models", "Bearer k")

    ollama = {"http://ol.test:11434/api/ps": {"models": [{"name": "qwen3:8b"}]}}
    with patch("app.services.llm_settings.urlopen", _fake_server(ollama, [])):
        assert find_loaded_model("http://ol.test:11434/v1/responses") == "qwen3:8b"


def test_loaded_model_without_loaded_model_keeps_placeholder(tmp_path):
    app = _create_app(
        tmp_path,
        CHAT_LLM_ENDPOINT_URL="http://studio.test/v1/responses",
        CHAT_LLM_MODEL="loaded",
    )
    routes = {"http://studio.test/api/v1/models": {"models": []}}
    with app.app_context(), patch(
        "app.services.llm_settings.urlopen", _fake_server(routes, [])
    ):
        endpoint = resolve_llm_endpoint(PURPOSE_CHAT)
    # Kein anderes Modell wählen: der Server soll nichts nachladen.
    assert endpoint.model == "loaded"


def test_user_access_with_loaded_model(tmp_path):
    app = _create_app(tmp_path)
    user_id = _seed_user(app, username="maria", role="Buchhalter")
    client = app.test_client()
    response = client.post(
        f"/api/v1/users/{user_id}/llm",
        json={
            "provider": "custom",
            "endpoint_url": "http://studio.test:1234/v1",
            "model": "loaded",
        },
    )
    assert response.status_code == 200
    routes = {
        "http://studio.test:1234/api/v1/models": {
            "models": [{"type": "llm", "key": "m", "loaded_instances": [{"id": "m"}]}]
        }
    }
    with app.app_context(), patch(
        "app.services.llm_settings.urlopen", _fake_server(routes, [])
    ):
        endpoint = resolve_llm_endpoint(PURPOSE_RECEIPT_OCR, user_id=user_id)
    assert endpoint.url == "http://studio.test:1234/v1/responses"
    assert endpoint.model == "m"
    assert endpoint.source == "user"


def test_llm_timeout_is_configurable(tmp_path):
    from app.services.llm_settings import llm_timeout

    assert llm_timeout(30) == 30
    with _create_app(tmp_path).app_context():
        assert llm_timeout(30) == 30
    with _create_app(tmp_path, LLM_TIMEOUT_SECONDS="90").app_context():
        assert llm_timeout(30) == 90.0


def _stream(entries: str, data: bytes) -> bytes:
    return f"<< {entries} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


def _pdf(*objects: bytes) -> bytes:
    """Minimal-PDF aus Objektrümpfen (Objekt 1 = Katalog, 2 = Seitenbaum)."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


_CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
_ONE_PAGE = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"


def _png_gray_rows(png: bytes) -> list[bytes]:
    """Pixelzeilen eines 8-Bit-Graustufen-PNGs aus ``_png_from_pixels``."""
    import struct
    import zlib

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, color_type = struct.unpack(">IIBB", png[16:26])
    assert (depth, color_type) == (8, 0)
    raw = zlib.decompress(png[png.index(b"IDAT") + 4 : png.index(b"IEND") - 8])
    rows = [raw[y * (width + 1) : (y + 1) * (width + 1)] for y in range(height)]
    assert all(row[0] == 0 for row in rows)
    return [row[1:] for row in rows]


def test_ocr_retries_vector_pdf_as_rendered_page(monkeypatch):
    # PDF ohne Textebene und ohne eingebettetes Bild (z. B. Text als Pfade):
    # Erst nach dem Rendern sieht der Endpoint überhaupt etwas.
    from urllib.error import HTTPError

    pdf = _pdf(
        _CATALOG,
        _ONE_PAGE,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Contents 4 0 R >>",
        _stream("", b"0 g 0 0 100 100 re f"),
    )
    payloads = []

    def fake_urlopen(request, timeout=0):
        payload = json.loads(request.data)
        payloads.append(payload)
        if payload["input"][1]["content"][0]["type"] == "input_file":
            raise HTTPError(request.full_url, 400, "Bad Request", {}, None)
        return _FakeResponse({"output_text": "Gesamt 19,98 EUR"})

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    text = receipt_ocr._ocr_via_endpoint(
        endpoint_url="http://studio.test/v1/responses",
        model="m",
        file_bytes=pdf,
        mime_type="application/pdf",
        file_name="vektor.pdf",
    )
    assert text == "Gesamt 19,98 EUR"
    blocks = payloads[1]["input"][1]["content"]
    assert [block["type"] for block in blocks] == ["input_image", "input_text"]
    prefix, encoded = blocks[0]["image_url"].split(",", 1)
    assert prefix == "data:image/png;base64"
    rows = _png_gray_rows(base64.b64decode(encoded))
    # Längere Kante auf 2000 px, höchstens Faktor 4: 200x100 pt -> 800x400 px.
    assert (len(rows[0]), len(rows)) == (800, 400)
    assert rows[200][100] < 64  # linke Hälfte schwarz gefüllt
    assert rows[200][700] > 192  # rechte Hälfte weiß


def test_pdf_page_images_render_scan_inside_form_xobject():
    # Scanner legen das Seitenbild oft in ein Form-XObject statt direkt auf die Seite.
    import zlib

    pdf = _pdf(
        _CATALOG,
        _ONE_PAGE,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 50] "
        b"/Resources << /XObject << /Fm0 5 0 R >> >> /Contents 4 0 R >>",
        _stream("", b"/Fm0 Do"),
        _stream(
            "/Type /XObject /Subtype /Form /BBox [0 0 100 50] "
            "/Resources << /XObject << /Im0 6 0 R >> >>",
            b"q 100 0 0 50 0 0 cm /Im0 Do Q",
        ),
        _stream(
            "/Type /XObject /Subtype /Image /Width 2 /Height 1 /ColorSpace /DeviceGray "
            "/BitsPerComponent 8 /Filter /FlateDecode",
            zlib.compress(bytes([0, 255])),
        ),
    )
    [(mime, png)] = receipt_ocr._pdf_page_images(pdf)
    assert mime == "image/png"
    rows = _png_gray_rows(png)
    assert (len(rows[0]), len(rows)) == (400, 200)
    assert rows[100][50] < 64
    assert rows[100][350] > 192


def test_pdf_page_images_of_broken_pdf_are_empty():
    assert receipt_ocr._pdf_page_images(b"%PDF-1.4 kaputt") == []


def test_ocr_http_error_without_page_images_is_reported(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout=0):
        raise HTTPError(request.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    with pytest.raises(receipt_ocr.ReceiptOCRError, match="HTTP 400"):
        receipt_ocr._ocr_via_endpoint(
            endpoint_url="http://studio.test/v1/responses",
            model="m",
            file_bytes=b"%PDF-1.4 kaputt",
            mime_type="application/pdf",
            file_name="scan.pdf",
        )


def test_ocr_read_timeout_is_reported_as_ocr_error(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(receipt_ocr, "urlopen", fake_urlopen)
    with pytest.raises(receipt_ocr.ReceiptOCRError, match="nicht rechtzeitig"):
        receipt_ocr._ocr_via_endpoint(
            endpoint_url="http://studio.test/v1/responses",
            model="m",
            file_bytes=b"\x89PNG",
            mime_type="image/png",
            file_name="scan.png",
        )


def test_chat_reply_excludes_reasoning_items():
    from app.services.chat_llm import run_chat_turn

    body = {
        "output": [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "The user wants ..."}],
                "summary": [{"type": "summary_text", "text": "Denkt nach"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Die Gesellschaft hat 3 Bankkonten."}],
            },
        ]
    }
    result = run_chat_turn(
        endpoint_url="http://studio.test/v1/responses",
        model="m",
        input_items=[],
        tools=[],
        execute_tool=lambda name, arguments: ("", False),
        post_json=lambda url, payload, **kwargs: body,
    )
    assert result.reply_text == "Die Gesellschaft hat 3 Bankkonten."


def test_llm_tools_have_long_mcp_timeout():
    from app.services.mcp_server import LLM_TOOL_TIMEOUT, TOOLS

    timeouts = {tool.name: tool.timeout for tool in TOOLS}
    for name in ("send_chat_message", "confirm_chat_action", "create_receipt_match_suggestion"):
        assert timeouts[name] == LLM_TOOL_TIMEOUT
