"""LLM-Client für den integrierten KI-Chat.

Spricht einen OpenAI-``/responses``-kompatiblen Endpoint (wie die übrigen
LLM-Anbindungen, nur stdlib) und implementiert darauf die Tool-Calling-Schleife:
Das Modell erhält die MCP-Tools als Funktionsdefinitionen, angeforderte
Tool-Aufrufe werden über einen Callback ausgeführt und als
``function_call_output`` zurückgereicht, bis das Modell eine finale
Textantwort liefert (oder das Aufruf-Limit erreicht ist).

Gegen ausufernde Modelle (z. B. ein Denkprozess in der Endlosschleife) gilt je
Modellaufruf ein Reasoning-Budget, das unabhängig vom Modell im Stream gezählt
wird, plus eine Obergrenze der gesamten Ausgabe, die auch als
``max_output_tokens`` an den Endpoint geht. Gegen tote Verbindungen bleibt nur ein
Leerlauf-Timeout; ein Gesamt-Zeitlimit je Chat-Zug gibt es nicht mehr.
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
    """Das Chat-LLM hat zu lange keine Daten geliefert (Leerlauf-Timeout)."""


class ChatLLMReasoningLimit(ChatLLMError):
    """Das Modell hat das Reasoning- bzw. Ausgabebudget eines Modellaufrufs überschritten."""


REASONING_LIMIT_TEXT = (
    "Obi hat zu lange nachgedacht und wurde abgebrochen – bitte die Frage neu "
    "oder genauer stellen."
)

# Leerlauf-Timeout je Modellaufruf (``CHAT_LLM_IDLE_TIMEOUT_SECONDS``): so lange
# darf der Endpoint keine Daten liefern. Großzügig, weil während der
# Prompt-Verarbeitung und solange ein lokaler Server (LM Studio: eine Anfrage
# gleichzeitig) eine fremde Anfrage abarbeitet, minutenlang nichts kommt.
DEFAULT_IDLE_TIMEOUT_SECONDS = 600.0
# Reasoning-Token je Modellaufruf (``CHAT_LLM_MAX_REASONING_TOKENS``, 0 = aus).
DEFAULT_MAX_REASONING_TOKENS = 16000
# Spielraum für Antwort und Tool-Aufrufe über dem Reasoning-Budget. Die Summe
# begrenzt die gesamte Ausgabe eines Modellaufrufs: clientseitig im Stream und als
# ``max_output_tokens`` beim Endpoint.
ANSWER_TOKEN_ALLOWANCE = 8000
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
    timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """POST an den ``/responses``-Endpoint, liefert den Antwort-Body.

    Mit ``on_event`` wird die Antwort gestreamt (``stream: true``, Server-Sent
    Events): Jedes Ereignis geht an ``on_event``, zurück kommt die vollständige
    Antwort aus ``response.completed``. Ein Endpoint ohne Stream-Unterstützung
    antwortet mit JSON, das wie bisher gelesen wird. ``timeout`` ist ein
    Leerlauf-Timeout: Er greift, wenn der Endpoint so lange keine Daten liefert.
    Eine Ausnahme aus ``on_event`` bricht den Aufruf ab und schließt die
    Verbindung (der Server hört dann auf zu generieren).
    """
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
                return _read_event_stream(response, on_event)
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


def _read_event_stream(response, on_event: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Liest einen ``/responses``-Stream (SSE) bis ``response.completed``."""
    data_lines: list[str] = []
    for raw_line in response:
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
            body = body if isinstance(body, dict) else {}
            if kind == "response.incomplete":
                body.setdefault("status", "incomplete")
            return body
        if kind in {"response.failed", "error"}:
            logger.warning("Chat-LLM meldete im Stream einen Fehler: %s", str(event)[:500])
            raise ChatLLMError("Chat-LLM meldete einen Fehler.")
        on_event(event)
    raise ChatLLMError("Chat-LLM hat die Antwort nicht abgeschlossen.")


_REASONING_DELTA_TYPES = {
    "response.reasoning_text.delta",  # LM Studio, vLLM u. a.
    "response.reasoning_summary_text.delta",  # OpenAI (nur die Zusammenfassung)
}


def _token_count(text: str) -> int:
    """Token eines Stream-Fragments.

    Die gängigen Server (LM Studio, OpenAI, Ollama, llama.cpp) streamen ein Token
    je Fragment; bündelt ein Server mehrere, wird vorsichtig nach Länge geschätzt.
    """
    return max(1, len(text) // 6)


class _StreamProgress:
    """Beobachtet den LLM-Stream eines Modellaufrufs: Fortschritt und Reasoning-Schranke.

    Meldet gedrosselt ``{"type": "generating", "round", "phase", "tokens",
    "seconds"}`` mit ``phase`` ``reasoning`` (samt ``snippet``, dem Ende des
    Denkprozesses) oder ``answer`` sowie ``{"type": "preparing_tool", "round",
    "name"}``, sobald das Modell einen Tool-Aufruf beginnt.

    Als Denkprozess zählen getypte Reasoning-Ereignisse und ``<think>``-Text in der
    Antwort (siehe :class:`_ThinkSplitter`). Mit ``max_reasoning_tokens`` wirft der
    Beobachter :class:`ChatLLMReasoningLimit`, sobald der Denkprozess das Budget
    oder die gesamte Ausgabe (Denkprozess, Antwort, Tool-Argumente) das Budget plus
    :data:`ANSWER_TOKEN_ALLOWANCE` überschreitet. Die zweite Grenze fängt Modelle,
    deren ``<think>`` schon im Prompt-Template steht: Ihr Denkprozess ist erst am
    ``</think>`` als solcher zu erkennen.
    """

    def __init__(
        self,
        report: Callable[[dict[str, Any]], None],
        round_: int,
        *,
        max_reasoning_tokens: int = 0,
    ) -> None:
        self.report = report
        self.round = round_
        self.max_reasoning_tokens = max_reasoning_tokens
        self.started = time.monotonic()
        self.last_report = 0.0
        self.phase = ""
        self.tokens = {"reasoning": 0, "answer": 0, "tool": 0}
        self.reasoning_tail = ""
        self.think = _ThinkSplitter()

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        delta = event.get("delta")
        text = delta if isinstance(delta, str) else ""
        if kind in _REASONING_DELTA_TYPES:
            self._count("reasoning", text)
        elif kind == "response.output_text.delta":
            parts = self.think.feed(text) if text else [("answer", "")]
            for phase, part in parts:
                if phase == "close":
                    # ``</think>`` ohne öffnendes Tag: Der bisherige Text war schon
                    # Denkprozess. Kein nachträglicher Abbruch – das Modell hat das
                    # Denken ja beendet; es gilt weiter die Obergrenze der Ausgabe.
                    self.tokens["reasoning"] += self.tokens["answer"]
                    self.tokens["answer"] = 0
                else:
                    self._count(phase, part)
        elif kind == "response.function_call_arguments.delta":
            self.tokens["tool"] += _token_count(text)
            self._check_budget("tool")
        elif kind == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name"):
                self.report(
                    {"type": "preparing_tool", "round": self.round, "name": str(item["name"])}
                )

    def _check_budget(self, phase: str) -> None:
        if self.max_reasoning_tokens <= 0:
            return
        if phase == "reasoning" and self.tokens["reasoning"] > self.max_reasoning_tokens:
            self._abort("Reasoning-Budget")
        if sum(self.tokens.values()) > self.max_reasoning_tokens + ANSWER_TOKEN_ALLOWANCE:
            self._abort("Ausgabe-Obergrenze")

    def _abort(self, limit: str) -> None:
        logger.warning(
            "Chat-LLM: Runde %s abgebrochen (%s überschritten): %s Reasoning-, %s Antwort-, "
            "%s Tool-Token in %s s",
            self.round,
            limit,
            self.tokens["reasoning"],
            self.tokens["answer"],
            self.tokens["tool"],
            round(time.monotonic() - self.started),
        )
        raise ChatLLMReasoningLimit(REASONING_LIMIT_TEXT)

    def _count(self, phase: str, text: str) -> None:
        if phase == "reasoning" and text:
            self.reasoning_tail = (self.reasoning_tail + text)[-4 * REASONING_SNIPPET_CHARS:]
        self.tokens[phase] += _token_count(text)
        self._check_budget(phase)
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


_THINK_TAG_RE = re.compile(r"<(/?)think>", re.IGNORECASE)
_THINK_TAGS = ("<think>", "</think>")


class _ThinkSplitter:
    """Teilt gestreamten Antworttext in Denkprozess und Antwort (wie :func:`_strip_think`).

    ``feed`` liefert je Fragment ``(art, text)``-Teile: ``reasoning`` innerhalb von
    ``<think>…</think>``, ``answer`` außerhalb sowie ``("close", "")`` für ein
    ``</think>`` ohne öffnendes Tag – dann war der bisherige Text schon
    Denkprozess. Ein Tag, das über die Fragmentgrenze reicht, wird bis zum
    nächsten Fragment zurückgehalten.
    """

    def __init__(self) -> None:
        self.inside = False
        self.pending = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        text = self.pending + text
        self.pending = ""
        parts: list[tuple[str, str]] = []
        while text:
            match = _THINK_TAG_RE.search(text)
            if match is None:
                keep = _partial_tag_length(text)
                self.pending = text[len(text) - keep:]
                text = text[: len(text) - keep]
                if text:
                    parts.append(("reasoning" if self.inside else "answer", text))
                break
            if match.start():
                parts.append(("reasoning" if self.inside else "answer", text[: match.start()]))
            if not match.group(1):
                self.inside = True
            elif self.inside:
                self.inside = False
            else:
                parts.append(("close", ""))
            text = text[match.end():]
        return parts


def _partial_tag_length(text: str) -> int:
    """Länge des längsten Textendes, mit dem ein ``<think>``-Tag beginnt."""
    for length in range(min(len(text), len(_THINK_TAGS[1]) - 1), 0, -1):
        tail = text[-length:].lower()
        if any(tag.startswith(tail) for tag in _THINK_TAGS):
            return length
    return 0


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


def _output_limit_reached(body: dict[str, Any], limit: int | None) -> bool:
    """Hat der Endpoint die Ausgabe bei ``max_output_tokens`` abgeschnitten?

    OpenAI meldet das als ``status: incomplete`` mit Grund ``max_output_tokens``.
    LM Studio schließt die Antwort dagegen normal ab (``completed``, ohne
    ``incomplete_details``); erkennbar ist das nur an ``usage.output_tokens`` am
    Limit (beobachtet: ein Token darunter, der Denkprozess zählt mit).
    """
    details = body.get("incomplete_details")
    if (
        body.get("status") == "incomplete"
        and isinstance(details, dict)
        and details.get("reason") == "max_output_tokens"
    ):
        return True
    usage = body.get("usage")
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    return (
        bool(limit)
        and isinstance(output_tokens, int)
        and output_tokens >= limit - max(8, limit // 100)
    )


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
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    max_reasoning_tokens: int = DEFAULT_MAX_REASONING_TOKENS,
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

    ``max_reasoning_tokens`` begrenzt den Denkprozess je Modellaufruf, die gesamte
    Ausgabe je Aufruf zusätzlich auf das Budget plus :data:`ANSWER_TOKEN_ALLOWANCE`
    (clientseitig im Stream und als ``max_output_tokens``); überschritten wirft
    der Zug :class:`ChatLLMReasoningLimit`, ``0`` schaltet beides ab.
    ``idle_timeout`` greift, wenn der Endpoint so lange keine Daten liefert; eine
    Höchstdauer des ganzen Zuges gibt es nicht.
    """
    report = on_progress or (lambda _event: None)
    poster = post_json or _post_json
    max_reasoning_tokens = max(0, max_reasoning_tokens)
    output_limit = (
        max_reasoning_tokens + ANSWER_TOKEN_ALLOWANCE if max_reasoning_tokens else None
    )
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
        if output_limit:
            # Serverseitige Schranke, auch für Anbieter, die den Denkprozess
            # nicht streamen (OpenAI o-/gpt-5-Modelle); LM Studio zählt ihn mit.
            payload["max_output_tokens"] = output_limit

        report({"type": "thinking", "round": _round})
        try:
            body = poster(
                endpoint_url,
                payload,
                api_key=api_key,
                timeout=idle_timeout,
                on_event=_StreamProgress(
                    report, _round, max_reasoning_tokens=max_reasoning_tokens
                ),
            )
        except ChatLLMTimeout as exc:
            raise ChatLLMTimeout(
                f"Chat-LLM hat {_format_minutes(idle_timeout)} lang keine Daten geliefert "
                "(KI-Server ausgelastet oder Verbindung unterbrochen). Bitte später "
                "erneut versuchen."
            ) from exc
        if _output_limit_reached(body, output_limit):
            logger.warning(
                "Chat-LLM: Runde %s am Ausgabelimit abgeschnitten (status=%s, usage=%s)",
                _round,
                body.get("status"),
                body.get("usage"),
            )
            raise ChatLLMReasoningLimit(REASONING_LIMIT_TEXT)
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
