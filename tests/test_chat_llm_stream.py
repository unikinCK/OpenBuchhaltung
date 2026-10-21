"""KI-Chat: gestreamte LLM-Antworten, Fortschritt, Reasoning-Schranke, Leerlauf-Timeout."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from app.services.chat import ChatCancelled
from app.services.chat_llm import (
    ANSWER_TOKEN_ALLOWANCE,
    REASONING_LIMIT_TEXT,
    ChatLLMError,
    ChatLLMReasoningLimit,
    ChatLLMTimeout,
    _post_json,
    _StreamProgress,
    _ThinkSplitter,
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
    """Lokaler Fake-Endpoint; ``state["reply"]`` = (Content-Type, Body).

    Ein ``bytes``-Body geht in einem Stück raus. Eine Liste von Chunks wird
    nacheinander gesendet (ohne Content-Length); ``state["pause"]`` hält nach dem
    ersten Chunk so viele Sekunden an. ``state["sent"]`` zählt die gesendeten
    Chunks, ``state["disconnected"]`` wird gesetzt, wenn der Client die
    Verbindung vorzeitig schließt.
    """
    state: dict = {"requests": [], "sent": 0, "pause": 0.0}
    state["disconnected"] = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 – http.server-API
            length = int(self.headers.get("Content-Length") or 0)
            state["requests"].append(json.loads(self.rfile.read(length)))
            content_type, body = state["reply"]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if isinstance(body, bytes):
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for index, chunk in enumerate(body):
                    if index == 1 and state["pause"]:
                        time.sleep(state["pause"])
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    state["sent"] += 1
                    time.sleep(0.002)
            except OSError:
                state["disconnected"].set()

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


def test_run_chat_turn_passes_idle_timeout_and_reports_it():
    timeouts: list[float] = []

    def slow_poster(_url, _payload, *, api_key=None, timeout=None, on_event=None):
        timeouts.append(timeout)
        raise ChatLLMTimeout("Chat-LLM-Endpoint antwortete nicht rechtzeitig.")

    with pytest.raises(ChatLLMTimeout, match="10 Minuten lang keine Daten"):
        run_chat_turn(
            endpoint_url="http://llm.invalid",
            model="m",
            input_items=[],
            tools=[],
            execute_tool=lambda _name, _args: ("", False),
            idle_timeout=600,
            post_json=slow_poster,
        )
    # Jeder Modellaufruf bekommt den vollen Leerlauf-Timeout, keine Restzeit.
    assert timeouts == [600]


def test_idle_timeout_aborts_stream_without_data(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        [
            _sse({"type": "response.created", "response": {"output": []}}),
            _sse({"type": "response.completed", "response": COMPLETED}),
        ],
    )
    llm_server["pause"] = 2.0
    started = time.monotonic()
    with pytest.raises(ChatLLMTimeout):
        _post_json(llm_server["url"], {"model": "m"}, timeout=0.3, on_event=lambda _e: None)
    assert time.monotonic() - started < 1.5


def test_idle_timeout_does_not_limit_a_stream_that_keeps_delivering(llm_server):
    deltas = [_sse({"type": "response.reasoning_text.delta", "delta": "x"})] * 150
    llm_server["reply"] = (
        "text/event-stream",
        [*deltas, _sse({"type": "response.completed", "response": COMPLETED})],
    )
    started = time.monotonic()
    body = _post_json(llm_server["url"], {"model": "m"}, timeout=0.2, on_event=lambda _e: None)
    assert body == COMPLETED
    # Insgesamt länger als der Leerlauf-Timeout, aber nie 0,2 s ohne Daten.
    assert time.monotonic() - started > 0.2


# --- Reasoning-Schranke ----------------------------------------------------


def _turn(post_json, **kwargs):
    return run_chat_turn(
        endpoint_url="http://llm.invalid",
        model="m",
        input_items=[],
        tools=[],
        execute_tool=lambda _name, _args: ("", False),
        post_json=post_json,
        **kwargs,
    )


def _stream_poster(events: list[dict], body: dict = COMPLETED, payloads: list | None = None):
    """Fake-Poster, der ``events`` an ``on_event`` gibt und dann ``body`` liefert."""

    def poster(_url, payload, *, api_key=None, timeout=None, on_event=None):
        if payloads is not None:
            payloads.append(payload)
        for event in events:
            on_event(event)
        return body

    return poster


def _reasoning(count: int, text: str = "Hm. ") -> list[dict]:
    return [{"type": "response.reasoning_text.delta", "delta": text}] * count


def test_typed_reasoning_over_budget_aborts_the_stream(llm_server):
    deltas = [_sse(event) for event in _reasoning(400)]
    llm_server["reply"] = (
        "text/event-stream",
        [*deltas, _sse({"type": "response.completed", "response": COMPLETED})],
    )
    with pytest.raises(ChatLLMReasoningLimit) as excinfo:
        _post_json(
            llm_server["url"],
            {"model": "m"},
            on_event=_StreamProgress(lambda _e: None, 0, max_reasoning_tokens=50),
        )
    assert str(excinfo.value) == REASONING_LIMIT_TEXT
    assert "zu lange nachgedacht" in str(excinfo.value)
    # Die Verbindung ist zu: Der Server kann nicht mehr alles ausliefern.
    assert llm_server["disconnected"].wait(5)
    assert llm_server["sent"] < 400


def test_typed_reasoning_over_budget_aborts_the_turn():
    with pytest.raises(ChatLLMReasoningLimit, match="zu lange nachgedacht"):
        _turn(_stream_poster(_reasoning(101)), max_reasoning_tokens=100)


def test_openai_reasoning_summary_counts_against_the_budget():
    events = [{"type": "response.reasoning_summary_text.delta", "delta": "Prüfe."}] * 11
    with pytest.raises(ChatLLMReasoningLimit):
        _turn(_stream_poster(events), max_reasoning_tokens=10)


def test_long_reasoning_under_budget_returns_the_answer():
    events = [
        *_reasoning(100),
        {"type": "response.output_text.delta", "delta": "Fertig."},
    ]
    result = _turn(_stream_poster(events), max_reasoning_tokens=100)
    assert result.reply_text == "Fertig."


def test_reasoning_budget_zero_switches_the_limit_off():
    payloads: list[dict] = []
    result = _turn(_stream_poster(_reasoning(5000), payloads=payloads), max_reasoning_tokens=0)
    assert result.reply_text == "Fertig."
    assert "max_output_tokens" not in payloads[0]


def test_think_text_over_budget_aborts():
    events = [
        {"type": "response.output_text.delta", "delta": "<think>"},
        *[{"type": "response.output_text.delta", "delta": "Hm, nochmal. "}] * 60,
    ]
    with pytest.raises(ChatLLMReasoningLimit):
        _turn(_stream_poster(events), max_reasoning_tokens=50)


def test_think_text_under_budget_returns_the_answer_without_thoughts():
    body = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "<think>Hm.</think>Fertig."}],
            }
        ]
    }
    events = [
        {"type": "response.output_text.delta", "delta": "<think>"},
        *[{"type": "response.output_text.delta", "delta": "Hm. "}] * 40,
        {"type": "response.output_text.delta", "delta": "</think>"},
        *[{"type": "response.output_text.delta", "delta": "Fertig. "}] * 40,
    ]
    result = _turn(_stream_poster(events, body), max_reasoning_tokens=50)
    assert result.reply_text == "Fertig."


def test_reasoning_with_template_opened_think_is_capped_by_the_output_limit():
    # ``<think>`` steht im Prompt-Template: Der Denkprozess kommt als normaler Text,
    # erst ein ``</think>`` würde ihn verraten. Die Obergrenze der Ausgabe greift.
    events = [{"type": "response.output_text.delta", "delta": "Hm."}] * (
        50 + ANSWER_TOKEN_ALLOWANCE + 1
    )
    with pytest.raises(ChatLLMReasoningLimit):
        _turn(_stream_poster(events), max_reasoning_tokens=50)


def test_template_opened_think_is_reclassified_at_the_closing_tag():
    reported: list[dict] = []
    progress = _StreamProgress(reported.append, 0, max_reasoning_tokens=50)
    for _ in range(30):
        progress({"type": "response.output_text.delta", "delta": "Hm. "})
    progress({"type": "response.output_text.delta", "delta": "</think>"})
    assert progress.tokens == {"reasoning": 30, "answer": 0, "tool": 0}
    # Das Denken ist beendet: kein nachträglicher Abbruch, die Antwort läuft weiter.
    for _ in range(30):
        progress({"type": "response.output_text.delta", "delta": "Fertig. "})
    assert progress.tokens == {"reasoning": 30, "answer": 30, "tool": 0}


def test_think_progress_shows_the_thought_as_reasoning():
    reported: list[dict] = []
    progress = _StreamProgress(reported.append, 1)
    progress({"type": "response.output_text.delta", "delta": "<think>Der "})
    assert reported == [
        {"type": "generating", "round": 1, "phase": "reasoning", "tokens": 1,
         "seconds": 0, "snippet": "Der"},
    ]


def test_tool_arguments_count_against_the_output_limit():
    events = [{"type": "response.function_call_arguments.delta", "delta": "{"}] * (
        10 + ANSWER_TOKEN_ALLOWANCE + 1
    )
    with pytest.raises(ChatLLMReasoningLimit):
        _turn(_stream_poster(events), max_reasoning_tokens=10)


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        (["<think>Hm</think>Ja"], [("reasoning", "Hm"), ("answer", "Ja")]),
        (["<thi", "nk>Hm</th", "ink>Ja"], [("reasoning", "Hm"), ("answer", "Ja")]),
        (["Hm.</think>", "Ja"], [("answer", "Hm."), ("close", ""), ("answer", "Ja")]),
        (["a <", "b"], [("answer", "a "), ("answer", "<b")]),
        (["<THINK>Hm</Think>Ja"], [("reasoning", "Hm"), ("answer", "Ja")]),
    ],
)
def test_think_splitter(chunks, expected):
    splitter = _ThinkSplitter()
    parts = [part for chunk in chunks for part in splitter.feed(chunk)]
    assert parts == expected


def test_run_chat_turn_sends_max_output_tokens():
    payloads: list[dict] = []
    _turn(_stream_poster([], payloads=payloads), max_reasoning_tokens=16000)
    assert payloads[0]["max_output_tokens"] == 16000 + ANSWER_TOKEN_ALLOWANCE


def test_incomplete_response_at_max_output_tokens_aborts(llm_server):
    incomplete = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": []}],
    }
    llm_server["reply"] = (
        "text/event-stream",
        _sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "Prüfe."},
            {"type": "response.incomplete", "response": incomplete},
        ),
    )
    with pytest.raises(ChatLLMReasoningLimit, match="zu lange nachgedacht"):
        run_chat_turn(
            endpoint_url=llm_server["url"],
            model="m",
            input_items=[],
            tools=[],
            execute_tool=lambda _name, _args: ("", False),
        )


def test_incomplete_event_without_status_counts_as_incomplete(llm_server):
    llm_server["reply"] = (
        "text/event-stream",
        _sse(
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            }
        ),
    )
    body = _post_json(llm_server["url"], {"model": "m"}, on_event=lambda _e: None)
    assert body["status"] == "incomplete"


def test_lmstudio_completion_at_the_output_limit_aborts():
    # LM Studio schneidet bei max_output_tokens ab, meldet aber ``completed`` ohne
    # incomplete_details – erkennbar nur an usage.output_tokens.
    body = {
        "status": "completed",
        "incomplete_details": None,
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Hm"}]}
        ],
        "usage": {
            "output_tokens": 100 + ANSWER_TOKEN_ALLOWANCE - 1,
            "output_tokens_details": {"reasoning_tokens": 100 + ANSWER_TOKEN_ALLOWANCE - 1},
        },
        "max_output_tokens": 100 + ANSWER_TOKEN_ALLOWANCE,
    }
    with pytest.raises(ChatLLMReasoningLimit):
        _turn(_stream_poster([], body), max_reasoning_tokens=100)


def test_completion_well_below_the_output_limit_is_a_normal_answer():
    body = {**COMPLETED, "status": "completed", "usage": {"output_tokens": 500}}
    assert _turn(_stream_poster([], body), max_reasoning_tokens=100).reply_text == "Fertig."


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


def test_batched_fragments_are_estimated_by_length():
    progress = _StreamProgress(lambda _e: None, 0, max_reasoning_tokens=100)
    # Ein Server, der viele Token je Fragment bündelt, entgeht der Schranke nicht.
    with pytest.raises(ChatLLMReasoningLimit):
        for _ in range(10):
            progress({"type": "response.reasoning_text.delta", "delta": "Hm, nochmal. " * 10})
