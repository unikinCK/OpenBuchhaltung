"""KI-Chat: Fortschritt als NDJSON-Stream und Abbrechen."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.services.chat import CANCELLED_REPLY_TEXT, ChatCancelled, run_chat_message
from app.services.chat_llm import run_chat_turn
from domain.models import ChatConversation, Company
from tests.test_chat import (
    _create_test_app,
    _FakePoster,
    _function_call_body,
    _logged_in_client,
    _message_body,
)

NDJSON = {"Accept": "application/x-ndjson"}


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.get_data(as_text=True).splitlines() if line]


def test_chat_send_streams_progress_and_result(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster(
        [
            _function_call_body("list_accounts", {"company_id": company_id}),
            _message_body("Keine Konten."),
        ]
    )
    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/chat/send",
            data={"company_id": str(company_id), "message": "Konten?"},
            headers=NDJSON,
        )
        events = _events(response)

    assert response.mimetype == "application/x-ndjson"
    assert response.headers["X-Accel-Buffering"] == "no"
    progress = [event["event"] for event in events if event["type"] == "progress"]
    assert [event["type"] for event in progress] == ["started", "thinking", "tool", "thinking"]
    assert progress[0]["user_message"]["content"] == "Konten?"
    assert progress[0]["created_conversation"] is True
    assert progress[2] == {"type": "tool", "name": "list_accounts", "title": "Konten auflisten"}
    assert progress[3]["round"] == 1
    done = events[-1]
    assert done["type"] == "done"
    assert done["result"]["assistant_message"]["content"] == "Keine Konten."


def test_chat_send_streams_errors(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    with patch("app.services.chat_llm._post_json", _FakePoster([])):
        events = _events(
            client.post(
                "/chat/send",
                data={"company_id": str(company_id), "message": "Hallo"},
                headers=NDJSON,
            )
        )
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 500  # Fake ohne Antwort: AssertionError im Zug


def test_chat_send_without_stream_header_stays_json(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)
    company_id = app.config["_TEST_COMPANY_ID"]
    with patch("app.services.chat_llm._post_json", _FakePoster([_message_body("Hi.")])):
        response = client.post(
            "/chat/send", data={"company_id": str(company_id), "message": "Hallo"}
        )
    assert response.mimetype == "application/json"
    assert response.get_json()["assistant_message"]["content"] == "Hi."


def test_run_chat_turn_reports_progress_and_can_be_stopped():
    events: list[dict] = []

    def stop_at_tool(event):
        events.append(event)
        if event["type"] == "tool":
            raise ChatCancelled()

    executed: list[str] = []
    poster = _FakePoster([_function_call_body("list_accounts", {})])
    with pytest.raises(ChatCancelled):
        run_chat_turn(
            endpoint_url="http://llm.invalid",
            model="m",
            input_items=[],
            tools=[],
            execute_tool=lambda name, _args: (executed.append(name), ("", False))[1],
            post_json=poster,
            on_progress=stop_at_tool,
        )
    assert events == [{"type": "thinking", "round": 0}, {"type": "tool", "name": "list_accounts"}]
    assert executed == []


def test_cancelled_message_is_answered_in_history(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = app.config["_TEST_COMPANY_ID"]
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        company = session.get(Company, company_id)
        session.expunge(company)

    def cancel_when_thinking(event):
        if event["type"] == "thinking":
            raise ChatCancelled()

    with app.app_context(), patch("app.services.chat_llm._post_json", _FakePoster([])):
        with pytest.raises(ChatCancelled):
            run_chat_message(
                session_factory=session_factory,
                company=company,
                conversation_id=None,
                message_text="Bitte abbrechen",
                uploads=[],
                api_user=None,
                global_access=True,
                progress=cancel_when_thinking,
            )

    with session_factory() as session:
        conversation = session.query(ChatConversation).one()
        assert [(m.role, m.content) for m in conversation.messages] == [
            ("user", "Bitte abbrechen"),
            ("assistant", CANCELLED_REPLY_TEXT),
        ]


def test_api_chat_message_streams_progress(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()
    company_id = app.config["_TEST_COMPANY_ID"]
    poster = _FakePoster(
        [
            _function_call_body("list_accounts", {"company_id": company_id}),
            _message_body("Keine Konten."),
        ]
    )
    with patch("app.services.chat_llm._post_json", poster):
        response = client.post(
            "/api/v1/chat/messages",
            json={"company_id": company_id, "message": "Konten?"},
            headers=NDJSON,
        )
        events = _events(response)

    assert response.mimetype == "application/x-ndjson"
    progress = [event["event"]["type"] for event in events if event["type"] == "progress"]
    assert progress == ["started", "thinking", "tool", "thinking"]
    assert events[-1]["type"] == "done"
    assert events[-1]["result"]["assistant_message"]["content"] == "Keine Konten."


def test_api_chat_message_stream_reports_chat_errors_as_422(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()
    company_id = app.config["_TEST_COMPANY_ID"]
    events = _events(
        client.post(
            "/api/v1/chat/messages",
            json={"company_id": company_id, "message": "Hallo", "conversation_id": 999999},
            headers=NDJSON,
        )
    )
    assert events[-1]["type"] == "error"
    assert events[-1]["status"] == 422
