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
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ChatLLMError(ValueError):
    """Raised when the chat LLM endpoint cannot be used."""


@dataclass(slots=True)
class ChatToolCall:
    """Ein vom Modell angeforderter und ausgeführter Tool-Aufruf."""

    name: str
    arguments: dict[str, Any]
    result_text: str
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "result_text": self.result_text,
            "is_error": self.is_error,
        }


@dataclass(slots=True)
class ChatTurnResult:
    """Ergebnis eines Chat-Zuges: finale Antwort plus ausgeführte Tool-Aufrufe."""

    reply_text: str
    tool_calls: list[ChatToolCall] = field(default_factory=list)


def _post_json(
    endpoint_url: str,
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
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
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ChatLLMError(f"Chat-LLM antwortete mit HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ChatLLMError("Chat-LLM-Endpoint ist nicht erreichbar.") from exc
    except json.JSONDecodeError as exc:
        raise ChatLLMError("Chat-LLM lieferte kein gültiges JSON.") from exc


def _output_items(body: dict[str, Any]) -> list[dict[str, Any]]:
    output = body.get("output")
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict)]
    return []


def _collect_message_text(body: dict[str, Any]) -> str:
    """Sammelt die finale Textantwort aus einem ``/responses``-Body."""
    if isinstance(body.get("output_text"), str) and body["output_text"].strip():
        return body["output_text"]
    parts: list[str] = []
    for item in _output_items(body):
        if item.get("type") not in {"message", None} and "content" not in item:
            continue
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(part for part in parts if part.strip())


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


def run_chat_turn(
    *,
    endpoint_url: str,
    model: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], tuple[str, bool]],
    api_key: str | None = None,
    max_tool_calls: int = 15,
    timeout: float = 120.0,
    post_json: Callable[..., dict[str, Any]] | None = None,
) -> ChatTurnResult:
    """Führt einen Chat-Zug inklusive Tool-Calling-Schleife aus.

    ``execute_tool(name, arguments)`` liefert ``(ergebnis_text, is_error)``.
    ``input_items`` wird in-place um Tool-Aufrufe/-Ergebnisse erweitert.
    """
    poster = post_json or _post_json
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

        body = poster(endpoint_url, payload, api_key=api_key, timeout=timeout)
        calls = _function_calls(body)
        if not calls:
            reply = _collect_message_text(body)
            if not reply.strip():
                raise ChatLLMError("Chat-LLM lieferte keine Antwort.")
            return ChatTurnResult(reply_text=reply, tool_calls=executed)

        for call in calls:
            name = str(call.get("name"))
            arguments = _parse_arguments(call.get("arguments"))
            call_id = call.get("call_id") or call.get("id") or f"call_{len(executed)}"

            if len(executed) >= max_tool_calls:
                limit_reached = True
                result_text = (
                    f"Tool-Limit erreicht ({max_tool_calls} Aufrufe pro Nachricht). "
                    "Bitte fasse die bisherigen Ergebnisse zusammen."
                )
                is_error = True
            else:
                result_text, is_error = execute_tool(name, arguments)
                executed.append(
                    ChatToolCall(
                        name=name,
                        arguments=arguments,
                        result_text=result_text,
                        is_error=is_error,
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

    raise ChatLLMError("Chat-LLM hat die Tool-Schleife nicht abgeschlossen.")
