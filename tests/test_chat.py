"""Tests für den integrierten KI-Chat (UI, API, Tool-Schleife, Anhänge)."""

import base64
import io
import json
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.auth import hash_password
from app.services.chat_llm import ChatLLMError
from domain.models import ChatConversation, ChatMessage, Company, Tenant, User


def _create_test_app(tmp_path: Path, **extra_config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_chat.db'}",
            "CHAT_LLM_ENDPOINT_URL": "http://llm.test/v1/responses",
            "CHAT_LLM_MODEL": "test-model",
            **extra_config,
        }
    )
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Chat Tenant")
        session.add(tenant)
        session.flush()
        company = Company(name="Chat GmbH", currency_code="EUR", tenant=tenant)
        session.add(company)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=tenant.id,
            )
        )
        session.commit()
        app.config["_TEST_COMPANY_ID"] = company.id
    return app


def _logged_in_client(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return client


def _message_body(text: str) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def _function_call_body(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "name": name,
                "arguments": json.dumps(arguments),
                "call_id": call_id,
            }
        ]
    }


class _FakePoster:
    """Liefert vorgegebene LLM-Antworten und protokolliert die Payloads."""

    def __init__(self, bodies: list[dict]):
        self.bodies = list(bodies)
        self.payloads: list[dict] = []

    def __call__(self, endpoint_url, payload, *, api_key=None, timeout=None):
        self.payloads.append(payload)
        if not self.bodies:
            raise AssertionError("Keine weitere Fake-LLM-Antwort vorbereitet.")
        return self.bodies.pop(0)


def test_chat_page_renders(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    response = client.get("/chat")

    assert response.status_code == 200
    assert "Wobei kann ich helfen?".encode() in response.data
    assert b"chat.js" in response.data


def test_chat_send_creates_conversation_and_reply(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Hallo, wie kann ich helfen?")])

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Hallo Assistent"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["created_conversation"] is True
    assert payload["assistant_message"]["content"] == "Hallo, wie kann ich helfen?"

    with app.extensions["db_session_factory"]() as session:
        conversation = session.get(ChatConversation, payload["conversation_id"])
        assert conversation is not None
        assert conversation.title == "Hallo Assistent"
        assert conversation.company_id == company_id
        roles = [message.role for message in conversation.messages]
        assert roles == ["user", "assistant"]

    # System-Prompt nennt die aktive Gesellschaft, Tools werden mitgesendet.
    first_payload = poster.payloads[0]
    system_text = first_payload["input"][0]["content"][0]["text"]
    assert f"company_id={company_id}" in system_text
    tool_names = {tool["name"] for tool in first_payload["tools"]}
    assert "list_accounts" in tool_names
    assert "send_chat_message" not in tool_names  # keine Chat-Rekursion


def test_chat_tool_loop_executes_internal_api(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster(
        [
            _function_call_body("list_accounts", {"company_id": company_id}),
            _message_body("Es sind keine Konten angelegt."),
        ]
    )

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Welche Konten gibt es?"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    tool_calls = payload["assistant_message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "list_accounts"
    assert tool_calls[0]["is_error"] is False

    # Der zweite LLM-Aufruf enthält das Tool-Ergebnis als function_call_output.
    second_payload = poster.payloads[1]
    outputs = [
        item
        for item in second_payload["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    assert "accounts" in outputs[0]["output"]


def test_chat_tool_loop_handles_unknown_tool(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster(
        [
            _function_call_body("does_not_exist", {}),
            _message_body("Das Tool gibt es nicht."),
        ]
    )

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Teste Unbekanntes"},
        )

    assert response.status_code == 200
    tool_calls = response.get_json()["assistant_message"]["tool_calls"]
    assert tool_calls[0]["is_error"] is True


def test_chat_send_with_text_attachment(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Die Notiz habe ich gelesen.")])

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={
                "company_id": str(company_id),
                "message": "Was steht in der Notiz?",
                "attachments": (io.BytesIO(b"Miete Februar 1200 EUR"), "notiz.txt"),
            },
        )

    assert response.status_code == 200
    payload = response.get_json()
    attachments = payload["user_message"]["attachments"]
    assert attachments[0]["file_name"] == "notiz.txt"
    assert attachments[0]["kind"] == "text"
    # Der Anhang-Text wird dem Modell übergeben, aber nicht an den Client gespiegelt.
    assert "text" not in attachments[0]
    user_blocks = poster.payloads[0]["input"][-1]["content"]
    combined = "\n".join(block.get("text", "") for block in user_blocks)
    assert "Miete Februar 1200 EUR" in combined


def test_chat_send_rejects_forbidden_attachment_type(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Ok.")])

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={
                "company_id": str(company_id),
                "message": "Hier eine Datei",
                "attachments": (io.BytesIO(b"MZ\x90\x00"), "tool.exe"),
            },
        )

    assert response.status_code == 200
    attachments = response.get_json()["user_message"]["attachments"]
    assert attachments[0]["kind"] == "error"


def test_chat_send_without_llm_config_fails(tmp_path):
    app = _create_test_app(tmp_path, CHAT_LLM_ENDPOINT_URL=None)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]

    response = client.post(
        "/chat/send", data={"company_id": str(company_id), "message": "Hallo"}
    )

    assert response.status_code == 400
    assert "CHAT_LLM_ENDPOINT_URL" in response.get_json()["error"]


def test_chat_send_llm_error_returns_502(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]

    def _raise(endpoint_url, payload, *, api_key=None, timeout=None):
        raise ChatLLMError("Chat-LLM-Endpoint ist nicht erreichbar.")

    with patch("app.services.chat_llm._post_json", _raise):
        response = client.post(
            "/chat/send", data={"company_id": str(company_id), "message": "Hallo"}
        )

    assert response.status_code == 502


def test_chat_conversation_continues_with_history(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Erste Antwort."), _message_body("Zweite Antwort.")])

    with patch("app.services.chat_llm._post_json", poster):
        first = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Erste Frage"},
        ).get_json()
        second = client.post(
            "/chat/send",
            data={
                "company_id": str(company_id),
                "conversation_id": str(first["conversation_id"]),
                "message": "Zweite Frage",
            },
        ).get_json()

    assert second["conversation_id"] == first["conversation_id"]
    assert second["created_conversation"] is False
    history_texts = json.dumps(poster.payloads[1]["input"], ensure_ascii=False)
    assert "Erste Frage" in history_texts
    assert "Erste Antwort." in history_texts


def test_chat_delete_conversation(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Antwort.")])

    with patch("app.services.chat_llm._post_json", poster):
        created = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Bitte löschen"},
        ).get_json()

    response = client.post(f"/chat/{created['conversation_id']}/delete")
    assert response.status_code == 302

    with app.extensions["db_session_factory"]() as session:
        assert session.get(ChatConversation, created["conversation_id"]) is None
        remaining = session.query(ChatMessage).count()
        assert remaining == 0


def test_api_chat_message_and_conversation_listing(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("API-Antwort.")])

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/api/v1/chat/messages",
            json={"company_id": company_id, "message": "Hallo über die API"},
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["assistant_message"]["content"] == "API-Antwort."
    conversation_id = payload["conversation_id"]

    listing = client.get(f"/api/v1/chat/conversations?company_id={company_id}")
    assert listing.status_code == 200
    ids = [row["id"] for row in listing.get_json()["conversations"]]
    assert conversation_id in ids

    detail = client.get(f"/api/v1/chat/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert [m["role"] for m in detail.get_json()["messages"]] == ["user", "assistant"]

    deleted = client.post(f"/api/v1/chat/conversations/{conversation_id}/delete")
    assert deleted.status_code == 200
    assert (
        client.get(f"/api/v1/chat/conversations/{conversation_id}").status_code == 404
    )


def test_api_chat_message_with_base64_attachment(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster([_message_body("Anhang gelesen.")])
    encoded = base64.b64encode(b"Beleg: Kaffee 4,20 EUR").decode("ascii")

    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/api/v1/chat/messages",
            json={
                "company_id": company_id,
                "message": "Was steht im Beleg?",
                "attachments": [
                    {"file_name": "beleg.txt", "content_base64": encoded}
                ],
            },
        )

    assert response.status_code == 200
    attachments = response.get_json()["user_message"]["attachments"]
    assert attachments[0]["kind"] == "text"


def test_chat_conversations_are_scoped_to_other_users(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = app.config["_TEST_COMPANY_ID"]
    with app.extensions["db_session_factory"]() as session:
        tenant_id = session.query(Tenant).first().id
        session.add(
            User(
                username="kollege",
                password_hash=hash_password("kollege123"),
                role="Buchhalter",
                tenant_id=tenant_id,
            )
        )
        session.commit()

    client = _logged_in_client(app)
    poster = _FakePoster([_message_body("Nur für admin.")])
    with patch("app.services.chat_llm._post_json", poster):
        created = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Privat"},
        ).get_json()

    other = app.test_client()
    other.post("/auth/login", data={"username": "kollege", "password": "kollege123"})
    page = other.get(f"/chat?company_id={company_id}")
    assert b"Privat" not in page.data

    foreign = other.get(
        f"/chat?company_id={company_id}&conversation_id={created['conversation_id']}"
    )
    assert "Nur für admin.".encode() not in foreign.data
