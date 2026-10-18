"""LLM-Client für den integrierten KI-Chat.

Spricht einen OpenAI-``/responses``-kompatiblen Endpoint (wie die übrigen
LLM-Anbindungen, nur stdlib) und implementiert darauf die Tool-Calling-Schleife:
Das Modell erhält die MCP-Tools als Funktionsdefinitionen, angeforderte
Tool-Aufrufe werden über einen Callback ausgeführt und als
``function_call_output`` zurückgereicht, bis das Modell eine finale
Textantwort liefert (oder das Aufruf-Limit erreicht ist).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class ChatLLMError(ValueError):
    """Raised when the chat LLM endpoint cannot be used."""


class ChatLLMTimeout(ChatLLMError):
    """Das Chat-LLM hat nicht innerhalb des Zeitlimits geantwortet."""


# Gesamtdauer eines Chat-Zuges (alle Modell- und Tool-Aufrufe), siehe
# ``CHAT_LLM_TIMEOUT_SECONDS``. Lokale Modelle brauchen für lange Antworten
# viele Minuten; der Fortschritt kommt währenddessen über den Stream.
DEFAULT_TURN_TIMEOUT_SECONDS = 1200.0
# Fortschritt aus dem LLM-Stream höchstens so oft melden.
STREAM_PROGRESS_INTERVAL_SECONDS = 1.0
# Länge des Denkprozess-Ausschnitts in der Fortschrittsanzeige.
REASONING_SNIPPET_CHARS = 160


# Status eines Tool-Aufrufs im Chat-Verlauf:
#   executed  – lesend, sofort ausgeführt
#   pending   – schreibend, wartet auf Bestätigung des Benutzers (Human-in-the-Loop)
#   deferred  – im selben Modell-Zug nach einer wartenden Aktion angefordert, nicht ausgeführt
#   confirmed – vom Benutzer bestätigt und ausgeführt
#   rejected  – vom Benutzer abgelehnt
TOOL_CALL_STATUS_EXECUTED = "executed"
TOOL_CALL_STATUS_PENDING = "pending"
TOOL_CALL_STATUS_DEFERRED = "deferred"
TOOL_CALL_STATUS_CONFIRMED = "confirmed"
TOOL_CALL_STATUS_REJECTED = "rejected"

PENDING_CONFIRMATION_TEXT = (
    "Wartet auf die Bestätigung des Benutzers. Die Aktion wurde noch nicht ausgeführt."
)
DEFERRED_CALL_TEXT = (
    "Nicht ausgeführt: Es wartet bereits eine Aktion auf Bestätigung. "
    "Bitte nach der Bestätigung erneut anfordern."
)


@dataclass(slots=True)
class ChatToolCall:
    """Ein vom Modell angeforderter Tool-Aufruf samt Ergebnis bzw. Status."""

    name: str
    arguments: dict[str, Any]
    result_text: str
    is_error: bool = False
    call_id: str = ""
    status: str = TOOL_CALL_STATUS_EXECUTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "result_text": self.result_text,
            "is_error": self.is_error,
            "call_id": self.call_id,
            "status": self.status,
        }


@dataclass(slots=True)
class ChatTurnResult:
    """Ergebnis eines Chat-Zuges: finale Antwort plus Tool-Aufrufe.

    ``pending_confirmation`` ist gesetzt, wenn der Zug bei einem schreibenden
    Tool-Aufruf angehalten wurde, der erst nach Bestätigung des Benutzers
    ausgeführt wird; ``reply_text`` kann dann leer sein.
    """

    reply_text: str
    tool_calls: list[ChatToolCall] = field(default_factory=list)
    pending_confirmation: bool = False


def _post_json(
    endpoint_url: str,
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
    timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """POST an den ``/responses``-Endpoint, liefert den Antwort-Body.

    Mit ``on_event`` wird die Antwort gestreamt (``stream: true``, Server-Sent
    Events): Jedes Ereignis geht an ``on_event``, zurück kommt die vollständige
    Antwort aus ``response.completed``. Ein Endpoint ohne Stream-Unterstützung
    antwortet mit JSON, das wie bisher gelesen wird. ``timeout`` begrenzt die
    Gesamtdauer des Aufrufs.
    """
    deadline = time.monotonic() + timeout
    if on_event is not None:
        payload = {**payload, "stream": True}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type") or ""
            if on_event is not None and "text/event-stream" in content_type:
                return _read_event_stream(response, on_event, deadline)
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Upstream-Fehlertexte nur ins Log, nicht an den Client (S10).
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        logger.warning("Chat-LLM antwortete mit HTTP %s: %s", exc.code, detail)
        raise ChatLLMError(f"Chat-LLM antwortete mit HTTP {exc.code}.") from exc
    except TimeoutError as exc:
        raise ChatLLMTimeout("Chat-LLM-Endpoint antwortete nicht rechtzeitig.") from exc
    except URLError as exc:
        raise ChatLLMError("Chat-LLM-Endpoint ist nicht erreichbar.") from exc
    except (OSError, HTTPException) as exc:
        raise ChatLLMError("Verbindung zum Chat-LLM wurde unterbrochen.") from exc
    except json.JSONDecodeError as exc:
        raise ChatLLMError("Chat-LLM lieferte kein gültiges JSON.") from exc


def _read_event_stream(
    response, on_event: Callable[[dict[str, Any]], None], deadline: float
) -> dict[str, Any]:
    """Liest einen ``/responses``-Stream (SSE) bis ``response.completed``."""
    data_lines: list[str] = []
    for raw_line in response:
        if time.monotonic() > deadline:
            raise TimeoutError
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
            continue
        if line or not data_lines:
            continue
        data = "\n".join(data_lines)
        data_lines = []
        if data == "[DONE]":
            break
        event = json.loads(data)
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete"}:
            body = event.get("response")
            return body if isinstance(body, dict) else {}
        if kind in {"response.failed", "error"}:
            logger.warning("Chat-LLM meldete im Stream einen Fehler: %s", str(event)[:500])
            raise ChatLLMError("Chat-LLM meldete einen Fehler.")
        on_event(event)
    raise ChatLLMError("Chat-LLM hat die Antwort nicht abgeschlossen.")


class _StreamProgress:
    """Übersetzt LLM-Stream-Ereignisse in gedrosselte Fortschrittsmeldungen.

    Meldet ``{"type": "generating", "round", "phase", "tokens", "seconds"}`` mit
    ``phase`` ``reasoning`` (samt ``snippet``, dem Ende des Denkprozesses) oder
    ``answer`` sowie ``{"type": "preparing_tool", "round", "name"}``, sobald das
    Modell einen Tool-Aufruf beginnt. ``tokens`` zählt die Stream-Fragmente
    (bei den gängigen Servern ein Token je Fragment).
    """

    def __init__(self, report: Callable[[dict[str, Any]], None], round_: int) -> None:
        self.report = report
        self.round = round_
        self.started = time.monotonic()
        self.last_report = 0.0
        self.phase = ""
        self.tokens = {"reasoning": 0, "answer": 0}
        self.reasoning_tail = ""

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in {"response.reasoning_text.delta", "response.reasoning_summary_text.delta"}:
            delta = event.get("delta")
            if isinstance(delta, str):
                self.reasoning_tail = (self.reasoning_tail + delta)[-4 * REASONING_SNIPPET_CHARS:]
            self._count("reasoning")
        elif kind == "response.output_text.delta":
            self._count("answer")
        elif kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
                self.report(
                    {"type": "preparing_tool", "round": self.round, "name": str(item["name"])}
                )

    def _count(self, phase: str) -> None:
        self.tokens[phase] += 1
        now = time.monotonic()
        if phase == self.phase and now - self.last_report < STREAM_PROGRESS_INTERVAL_SECONDS:
            return
        self.phase = phase
        self.last_report = now
        event: dict[str, Any] = {
            "type": "generating",
            "round": self.round,
            "phase": phase,
            "tokens": self.tokens[phase],
            "seconds": round(now - self.started),
        }
        if phase == "reasoning":
            event["snippet"] = _snippet(self.reasoning_tail)
        self.report(event)


def _snippet(text: str) -> str:
    """Ende eines Textes, ab einer Wortgrenze, mit zusammengefassten Leerzeichen."""
    words = " ".join(text.split())
    if len(words) <= REASONING_SNIPPET_CHARS:
        return words
    tail = words[-REASONING_SNIPPET_CHARS:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < 40 else tail


def _output_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    output = body.get("output")
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict)]
    return []


_REASONING_BLOCK_TYPES = {"reasoning_text", "summary_text"}

# Denkprozess, den ein Modell ohne Reasoning-Parser als Text liefert: vollständige
# ``<think>…</think>``-Blöcke sowie Text vor einem einzelnen ``</think>`` (das
# öffnende Tag steht dann schon im Prompt-Template, z. B. bei Nemotron/Qwen).
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


def _strip_think(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    closing = list(_THINK_CLOSE_RE.finditer(text))
    if closing:
        text = text[closing[-1].end():]
    return text.strip()


def _message_parts(item: dict[str, Any]) -> list[str]:
    content = item.get("content")
    if isinstance(content, str):
        return [content]
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and block.get("type") not in _REASONING_BLOCK_TYPES
                and block["text"].strip()
            ):
                parts.append(block["text"])
    return parts


def _output_shape(body: dict[str, Any]) -> list[str]:
    """Struktur der Ausgabe (Typen und Längen, kein Inhalt) fürs Diagnose-Log."""
    shape = []
    for item in _output_items(body):
        content = item.get("content")
        if isinstance(content, list):
            blocks = ",".join(
                f"{block.get('type')}:{len(str(block.get('text') or ''))}"
                for block in content
                if isinstance(block, dict)
            )
        else:
            blocks = f"str:{len(str(content or ''))}"
        shape.append(f"{item.get('type')}/{item.get('phase') or '-'}[{blocks}]")
    return shape


def _collect_message_text(body: dict[str, Any]) -> str:
    """Sammelt die finale Textantwort aus einem ``/responses``-Body.

    Reasoning gehört nicht in die Antwort: ``reasoning``-Elemente und
    ``reasoning_text``-/``summary_text``-Blöcke (LM Studio), Zwischen-Nachrichten
    mit ``phase="commentary"`` (OpenAI) sowie ``<think>``-Text werden verworfen.
    Liefert ein Endpoint mehrere Antwort-Nachrichten, zählt nur die letzte: Bei
    LM Studio (Nemotron) stand vereinzelt der Denkprozess als eigener Textteil
    vor der eigentlichen Antwort. Solche Antworten werden mit ihrer Struktur
    (ohne Inhalt) protokolliert.
    """
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return _strip_think(body["output_text"])
    messages: list[str] = []
    text_parts = 0
    for item in _output_items(body):
        if item.get("type") == "reasoning" or (
            item.get("type") not in {"message", None} and "content" not in item
        ):
            continue
        if item.get("phase") == "commentary":
            continue
        parts = _message_parts(item)
        text_parts += len(parts)
        text = _strip_think("\n".join(parts))
        if text:
            messages.append(text)
    if text_parts > 1:
        logger.info(
            "Chat-LLM-Antwort mit mehreren Textteilen, verwendet wird die letzte "
            "Nachricht: %s",
            _output_shape(body),
        )
    return messages[-1] if messages else ""


def _function_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    calls = []
    for item in _output_items(body):
        if item.get("type") == "function_call" and item.get("name"):
            calls.append(item)
    return calls


def _parse_arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _format_minutes(seconds: float) -> str:
    if seconds < 120:
        return f"{round(seconds)} Sekunden"
    return f"{round(seconds / 60)} Minuten"


def run_chat_turn(
    *,
    endpoint_url: str,
    model: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], tuple[str, bool]],
    api_key: str | None = None,
    max_tool_calls: int = 15,
    timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    post_json: Callable[..., dict[str, Any]] | None = None,
    requires_confirmation: Callable[[str, dict[str, Any]], bool] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> ChatTurnResult:
    """Führt einen Chat-Zug inklusive Tool-Calling-Schleife aus.

    ``execute_tool(name, arguments)`` liefert ``(ergebnis_text, is_error)``.
    ``requires_confirmation(name, arguments)`` markiert Tool-Aufrufe, die nicht
    sofort ausgeführt werden dürfen: Der erste solche Aufruf eines Modell-Zuges
    wird als ``pending`` protokolliert, alle danach angeforderten Aufrufe als
    ``deferred``; die Schleife endet dann mit ``pending_confirmation=True``.
    ``input_items`` wird in-place um Tool-Aufrufe/-Ergebnisse erweitert.
    ``on_progress(event)`` meldet vor jedem Modellaufruf ``{"type": "thinking",
    "round": n}``, währenddessen den Stream-Fortschritt (``generating``,
    ``preparing_tool``, siehe :class:`_StreamProgress`) und vor jeder
    Tool-Ausführung ``{"type": "tool", "name": …}``; eine Ausnahme daraus (z. B.
    Abbruch durch den Benutzer) beendet den Zug, auch mitten im Modellaufruf.
    ``timeout`` begrenzt die Gesamtdauer des Zuges.
    """
    report = on_progress or (lambda _event: None)
    poster = post_json or _post_json
    deadline = time.monotonic() + timeout
    executed: list[ChatToolCall] = []
    limit_reached = False

    # Harte Obergrenze an Runden, damit ein Modell, das trotz Limit-Hinweis
    # weiter Tools anfordert, die Schleife nicht endlos hält.
    for _round in range(max_tool_calls + 3):
        payload: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "metadata": {"source": "openbuchhaltung-chat"},
        }
        if tools and not limit_reached:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        report({"type": "thinking", "round": _round})
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise ChatLLMTimeout("Zeitlimit erreicht.")
            body = poster(
                endpoint_url,
                payload,
                api_key=api_key,
                timeout=remaining,
                on_event=_StreamProgress(report, _round),
            )
        except ChatLLMTimeout as exc:
            raise ChatLLMTimeout(
                f"Chat-LLM hat nicht innerhalb von {_format_minutes(timeout)} "
                "geantwortet. Bitte die Frage enger fassen oder später erneut versuchen."
            ) from exc
        calls = _function_calls(body)
        if not calls:
            reply = _collect_message_text(body)
            if not reply.strip():
                raise ChatLLMError("Chat-LLM lieferte keine Antwort.")
            return ChatTurnResult(reply_text=reply, tool_calls=executed)

        pending_found = False
        for call in calls:
            name = str(call.get("name"))
            arguments = _parse_arguments(call.get("arguments"))
            call_id = str(call.get("call_id") or call.get("id") or f"call_{len(executed)}")

            if len(executed) >= max_tool_calls:
                limit_reached = True
                result_text = (
                    f"Tool-Limit erreicht ({max_tool_calls} Aufrufe pro Nachricht). "
                    "Bitte fasse die bisherigen Ergebnisse zusammen."
                )
                is_error = True
            else:
                status = TOOL_CALL_STATUS_EXECUTED
                if pending_found:
                    result_text, is_error = DEFERRED_CALL_TEXT, True
                    status = TOOL_CALL_STATUS_DEFERRED
                elif requires_confirmation is not None and requires_confirmation(
                    name, arguments
                ):
                    result_text, is_error = PENDING_CONFIRMATION_TEXT, False
                    status = TOOL_CALL_STATUS_PENDING
                    pending_found = True
                else:
                    report({"type": "tool", "name": name})
                    result_text, is_error = execute_tool(name, arguments)
                executed.append(
                    ChatToolCall(
                        name=name,
                        arguments=arguments,
                        result_text=result_text,
                        is_error=is_error,
                        call_id=call_id,
                        status=status,
                    )
                )

            input_items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                }
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_text,
                }
            )

        if pending_found:
            # Human-in-the-Loop: Der Zug endet hier; nach Bestätigung/Ablehnung
            # wird die Schleife mit dem Ergebnis fortgesetzt.
            return ChatTurnResult(
                reply_text=_collect_message_text(body),
                tool_calls=executed,
                pending_confirmation=True,
            )

    raise ChatLLMError("Chat-LLM hat die Tool-Schleife nicht abgeschlossen.")
