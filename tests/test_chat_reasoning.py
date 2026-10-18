"""Der Denkprozess des Modells darf nicht in der Chat-Antwort landen."""

import json
import logging
from pathlib import Path

from app.services.chat_llm import run_chat_turn

FIXTURES = Path(__file__).parent / "fixtures"

ANSWER = "Die Demo GmbH hat ein Bankkonto mit 26.061,01 € Buchsaldo."
REASONING_DE = (
    "Der Benutzer fragt nach dem aktuellen Saldo der Bankkonten. Das Dashboard "
    "liefert eine Übersicht mit den Bankkonten. Ich sollte den Saldo nennen."
)


def _reply(body: dict) -> str:
    return run_chat_turn(
        endpoint_url="http://studio.test/v1/responses",
        model="loaded",
        input_items=[],
        tools=[],
        execute_tool=lambda name, arguments: ("", False),
        post_json=lambda url, payload, **kwargs: body,
    ).reply_text


def _message(text: str, **extra) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
        **extra,
    }


def test_lmstudio_raw_response_after_tool_call_keeps_only_answer():
    # Echte Antwort von LM Studio (nvidia/nemotron-3.5-super-vl) auf die Runde
    # nach get_dashboard; nur die 111 Tool-Definitionen sind entfernt.
    body = json.loads((FIXTURES / "lmstudio_responses_after_tool_call.json").read_text())
    reasoning = body["output"][0]["content"][0]["text"]

    reply = _reply(body)

    assert reply.startswith("Die Demo GmbH hat ein Bankkonto:")
    assert reasoning[:40] not in reply
    assert "Der Benutzer fragt" not in reply


def test_reasoning_as_separate_message_before_answer_is_dropped(caplog):
    body = {"output": [_message(REASONING_DE), _message(ANSWER)]}

    with caplog.at_level(logging.INFO, logger="app.services.chat_llm"):
        reply = _reply(body)

    assert reply == ANSWER
    # Diagnose ohne Inhalt: nur Typen und Längen.
    assert "message/-[output_text:" in caplog.text
    assert "Der Benutzer" not in caplog.text


def test_commentary_phase_message_is_dropped():
    body = {
        "output": [
            _message("Ich schaue im Dashboard nach.", phase="commentary"),
            _message(ANSWER, phase="final_answer"),
        ]
    }

    assert _reply(body) == ANSWER


def test_think_tags_in_answer_text_are_removed():
    assert _reply({"output": [_message(f"<think>{REASONING_DE}</think>\n\n{ANSWER}")]}) == ANSWER
    # Öffnendes Tag stand bereits im Prompt-Template, nur </think> im Text.
    assert _reply({"output": [_message(f"{REASONING_DE}\n</think>\n{ANSWER}")]}) == ANSWER
    assert _reply({"output_text": f"<think>{REASONING_DE}</think>{ANSWER}"}) == ANSWER


def test_multiple_text_blocks_of_one_message_stay_together():
    body = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "Teil 1."},
                    {"type": "output_text", "text": "Teil 2."},
                ],
            }
        ]
    }

    assert _reply(body) == "Teil 1.\nTeil 2."
