"""KI-Chat: gestreamte LLM-Antworten, Fortschritt und Gesamt-Zeitlimit."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from app.services.chat import ChatCancelled
from app.services.chat_llm import (
    ChatLLMError,
    ChatLLMTimeout,
    _post_json,
    _StreamProgress,
    run_chat_turn,
)

COMPLETED = {
    "output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Hmm."}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Fertig."}]},
    ]
}


def _sse(*events: dict) -> bytes:
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events
    )


@pytest.fixture
def llm_server():
    """Lokaler Fake-Endpoint; ``state["reply"]`` = (Content-Type, Body)."""
    state: dict = {"requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 – http.server-API
            length = int(self.headers.get("Content-Length") or 0)
            state["requests"].append(json.loads(self.rfile.read(length)))
            content_type, body = state["reply"]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
    yield state
    server.shutdown()
    server.server_close()


def test_post_json_streams_events_and_returns_completed_response(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        _sse(
            {"type": "response.created", "response": {"output": []}},
            {"type": "response.reasoning_text.delta", "delta": "Hm"},
            {"type": "response.output_text.delta", "delta": "Fertig."},
            {"type": "response.completed", "response": COMPLETED},
        ),
    )
    events: list[dict] = []
    body = _post_json(llm_server["url"], {"model": "m"}, on_event=events.append)

    assert body == COMPLETED
    assert llm_server["requests"][0]["stream"] is True
    assert [event["type"] for event in events] == [
        "response.created",
        "response.reasoning_text.delta",
        "response.output_text.delta",
    ]


def test_post_json_reads_plain_json_from_endpoint_without_stream(llm_server):
    llm_server["reply"] = ("application/json", json.dumps(COMPLETED).encode())
    events: list[dict] = []
    assert _post_json(llm_server["url"], {"model": "m"}, on_event=events.append) == COMPLETED
    assert events == []


def test_post_json_reports_failed_stream(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        _sse({"type": "response.failed", "response": {"error": {"message": "intern"}}}),
    )
    with pytest.raises(ChatLLMError, match="meldete einen Fehler"):
        _post_json(llm_server["url"], {"model": "m"}, on_event=lambda _event: None)


def test_post_json_rejects_stream_without_completion(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        _sse({"type": "response.output_text.delta", "delta": "Halb"}),
    )
    with pytest.raises(ChatLLMError, match="nicht abgeschlossen"):
        _post_json(llm_server["url"], {"model": "m"}, on_event=lambda _event: None)


def test_cancel_during_stream_stops_the_call(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        _sse(
            {"type": "response.reasoning_text.delta", "delta": "Hm"},
            {"type": "response.completed", "response": COMPLETED},
        ),
    )

    def cancel(_event):
        raise ChatCancelled()

    with pytest.raises(ChatCancelled):
        _post_json(llm_server["url"], {"model": "m"}, on_event=cancel)


def test_stream_progress_is_throttled_and_reports_phases():
    reported: list[dict] = []
    clock = iter([0.0, 0.1, 0.2, 0.3, 1.5, 1.6, 1.7])
    with patch("app.services.chat_llm.time.monotonic", lambda: next(clock)):
        progress = _StreamProgress(reported.append, 2)  # started = 0.0
        for delta in ["Der ", "Benutzer ", "fragt"]:  # 0.1 meldet, 0.2/0.3 gedrosselt
            progress({"type": "response.reasoning_text.delta", "delta": delta})
        progress({"type": "response.reasoning_text.delta", "delta": "."})  # 1.5: wieder fällig
        progress({"type": "response.output_text.delta", "delta": "Ant"})  # Phasenwechsel
        progress({"type": "response.output_text.delta", "delta": "wort"})  # gedrosselt
        progress(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "name": "list_accounts"},
            }
        )

    assert reported == [
        {"type": "generating", "round": 2, "phase": "reasoning", "tokens": 1,
         "seconds": 0, "snippet": "Der"},
        {"type": "generating", "round": 2, "phase": "reasoning", "tokens": 4,
         "seconds": 2, "snippet": "Der Benutzer fragt."},
        {"type": "generating", "round": 2, "phase": "answer", "tokens": 1, "seconds": 2},
        {"type": "preparing_tool", "round": 2, "name": "list_accounts"},
    ]


def test_stream_progress_snippet_keeps_only_the_end():
    reported: list[dict] = []
    progress = _StreamProgress(reported.append, 0)
    progress({"type": "response.reasoning_text.delta", "delta": "wort " * 100 + "Ende"})
    snippet = reported[0]["snippet"]
    assert snippet.endswith("wort Ende")
    assert len(snippet) <= 160
    assert snippet.startswith("wort")


def test_run_chat_turn_passes_remaining_time_and_reports_timeout():
    timeouts: list[float] = []

    def slow_poster(_url, _payload, *, api_key=None, timeout=None, on_event=None):
        timeouts.append(timeout)
        raise ChatLLMTimeout("Chat-LLM-Endpoint antwortete nicht rechtzeitig.")

    with pytest.raises(ChatLLMTimeout, match="nicht innerhalb von 20 Minuten"):
        run_chat_turn(
            endpoint_url="http://llm.invalid",
            model="m",
            input_items=[],
            tools=[],
            execute_tool=lambda _name, _args: ("", False),
            timeout=1200,
            post_json=slow_poster,
        )
    assert 1190 < timeouts[0] <= 1200


def test_run_chat_turn_forwards_stream_progress():
    events: list[dict] = []

    def streaming_poster(_url, _payload, *, api_key=None, timeout=None, on_event=None):
        on_event({"type": "response.reasoning_text.delta", "delta": "Überlege"})
        return COMPLETED

    result = run_chat_turn(
        endpoint_url="http://llm.invalid",
        model="m",
        input_items=[],
        tools=[],
        execute_tool=lambda _name, _args: ("", False),
        post_json=streaming_poster,
        on_progress=events.append,
    )
    assert result.reply_text == "Fertig."
    assert events == [
        {"type": "thinking", "round": 0},
        {"type": "generating", "round": 0, "phase": "reasoning", "tokens": 1,
         "seconds": 0, "snippet": "Überlege"},
    ]
