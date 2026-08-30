"""Integrierter KI-Chat: Unterhaltungen, Tool-Ausführung und Anhänge.

Der Chat nutzt die MCP-Tool-Registry (:mod:`app.services.mcp_server`) und führt
Tool-Aufrufe in-process gegen die eigene REST-API aus: Ein
:class:`InProcessApiClient` ersetzt den HTTP-Client des MCP-Servers durch
Flask-Testclient-Aufrufe, deren Auth-Kontext über einen (extern nicht
fälschbaren) WSGI-environ-Eintrag transportiert wird. Damit gelten für jeden
Tool-Aufruf exakt die Tenant-/Rollen-Regeln der REST-API.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app

from app.services.chat_llm import ChatLLMError, run_chat_turn
from app.services.documents import (
    DOCUMENT_SIGNATURE_PROBE_BYTES,
    document_content_error_code,
)
from app.services.mcp_server import TOOLS, ApiResponse, MCPServer
from app.services.receipt_ocr import (
    ReceiptOCRError,
    extract_document_text,
    sanitize_text,
)
from domain.models import ChatConversation, ChatMessage, Company

# Chat-eigene MCP-Tools nicht an das Modell geben (Rekursionsgefahr).
CHAT_TOOL_NAMES = {
    "list_chat_conversations",
    "get_chat_conversation",
    "send_chat_message",
    "delete_chat_conversation",
}

# Für Chat-Anhänge erlaubte Dateitypen (Erweiterung -> MIME-Typ).
CHAT_ATTACHMENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
}

# Zeichen-Limits, damit weder LLM-Kontext noch DB ausufern.
ATTACHMENT_TEXT_LIMIT = 20_000
TOOL_RESULT_LLM_LIMIT = 20_000
TOOL_RESULT_STORE_LIMIT = 6_000

MAX_TITLE_LENGTH = 80


class ChatError(ValueError):
    """Fachlicher Fehler im Chat (ungültige Eingabe, fehlende Konfiguration)."""


class InProcessApiClient:
    """Duck-Type-Ersatz für ``HttpApiClient``: ruft die eigene REST-API in-process auf.

    Der Auth-Kontext (API-Benutzer bzw. globaler Zugriff) wird über den
    WSGI-environ-Eintrag ``openbuchhaltung.internal_api`` an
    :func:`app.auth.require_api_token` übergeben. Externe Requests können nur
    ``HTTP_*``-Schlüssel setzen und diesen Eintrag daher nicht fälschen.
    """

    def __init__(self, app, *, api_user: dict | None, global_access: bool) -> None:
        self._app = app
        self._environ = {
            "openbuchhaltung.internal_api": {
                "user": dict(api_user) if api_user else None,
                "global_access": global_access,
            }
        }

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> ApiResponse:
        client = self._app.test_client()
        kwargs: dict[str, Any] = {
            "method": method,
            "query_string": params or None,
            "environ_base": dict(self._environ),
        }
        if json_body is not None:
            kwargs["json"] = json_body
        response = client.open(f"/api/v1{path}", **kwargs)
        text = response.get_data(as_text=True)
        content_type = response.headers.get("Content-Type", "")
        parsed = None
        if "application/json" in content_type:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        return ApiResponse(
            status=response.status_code, text=text, content_type=content_type, json=parsed
        )


def chat_tool_definitions() -> list[dict[str, Any]]:
    """MCP-Tools als Funktionsdefinitionen der OpenAI-``/responses``-API."""
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        }
        for tool in TOOLS
        if tool.name not in CHAT_TOOL_NAMES
    ]


def build_tool_executor(*, api_user: dict | None, global_access: bool):
    """Erzeugt den ``execute_tool``-Callback für :func:`run_chat_turn`."""
    server = MCPServer(
        http=InProcessApiClient(
            current_app._get_current_object(),
            api_user=api_user,
            global_access=global_access,
        )
    )

    def execute_tool(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if name in CHAT_TOOL_NAMES:
            return (f"Das Tool {name!r} steht im Chat nicht zur Verfügung.", True)
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        if not isinstance(response, dict) or "error" in response:
            detail = (response or {}).get("error", {}).get("message", "Unbekannter Fehler")
            return (f"Tool-Aufruf fehlgeschlagen: {detail}", True)
        result = response.get("result") or {}
        parts = [
            block.get("text", "")
            for block in result.get("content", [])
            if isinstance(block, dict)
        ]
        text = sanitize_text("\n".join(parts))
        if len(text) > TOOL_RESULT_LLM_LIMIT:
            text = text[:TOOL_RESULT_LLM_LIMIT] + "\n… [Ergebnis gekürzt]"
        return text, bool(result.get("isError"))

    return execute_tool


# ---------------------------------------------------------------------------
# Anhänge
# ---------------------------------------------------------------------------


def process_attachment(*, file_name: str, data: bytes) -> dict[str, Any]:
    """Prüft einen Chat-Anhang und extrahiert seinen Inhalt.

    Rückgabe: ``{file_name, mime_type, size, kind, text?, error?}`` mit
    ``kind`` in ``"text"`` (extrahierter Text), ``"image"`` (wird dem Modell als
    Bild übergeben) oder ``"error"``.
    """
    extension = Path(file_name).suffix.lower()
    mime_type = CHAT_ATTACHMENT_TYPES.get(extension)
    meta: dict[str, Any] = {
        "file_name": file_name,
        "mime_type": mime_type or "application/octet-stream",
        "size": len(data),
    }
    if mime_type is None:
        allowed = ", ".join(sorted(CHAT_ATTACHMENT_TYPES))
        meta.update(kind="error", error=f"Dateityp nicht erlaubt (erlaubt: {allowed}).")
        return meta

    max_bytes = current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES")
    if max_bytes and len(data) > max_bytes:
        meta.update(kind="error", error="Die Datei ist zu groß.")
        return meta
    if not data:
        meta.update(kind="error", error="Die Datei ist leer.")
        return meta

    if mime_type in {"application/pdf", "image/png", "image/jpeg"}:
        signature_error = document_content_error_code(
            mime_type=mime_type,
            content_head=data[:DOCUMENT_SIGNATURE_PROBE_BYTES],
            content_size=len(data),
            max_bytes=max_bytes,
            min_bytes=None,
        )
        if signature_error == "signature_mismatch":
            meta.update(
                kind="error",
                error="Der Dateiinhalt passt nicht zum Dateityp.",
            )
            return meta

    if mime_type in {"image/png", "image/jpeg"}:
        meta.update(kind="image")
        return meta

    try:
        text, _source = extract_document_text(
            file_bytes=data,
            mime_type=mime_type,
            file_name=file_name,
            ocr_endpoint=current_app.config.get("RECEIPT_OCR_ENDPOINT_URL"),
            ocr_model=current_app.config.get("RECEIPT_OCR_MODEL") or "gpt-4.1-mini",
        )
    except ReceiptOCRError as exc:
        meta.update(kind="error", error=str(exc))
        return meta

    if len(text) > ATTACHMENT_TEXT_LIMIT:
        text = text[:ATTACHMENT_TEXT_LIMIT] + "\n… [Inhalt gekürzt]"
    meta.update(kind="text", text=text)
    return meta


def _attachment_content_blocks(
    attachments: list[dict[str, Any]], image_data: dict[str, bytes]
) -> list[dict[str, Any]]:
    """Baut die ``input_text``/``input_image``-Blöcke für neue Anhänge."""
    blocks: list[dict[str, Any]] = []
    for meta in attachments:
        name = meta.get("file_name", "Datei")
        kind = meta.get("kind")
        if kind == "text":
            blocks.append(
                {
                    "type": "input_text",
                    "text": f"[Anhang {name}]\n{meta.get('text', '')}",
                }
            )
        elif kind == "image" and name in image_data:
            encoded = base64.b64encode(image_data[name]).decode("ascii")
            blocks.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{meta['mime_type']};base64,{encoded}",
                }
            )
            blocks.append({"type": "input_text", "text": f"[Anhang (Bild): {name}]"})
        elif kind == "error":
            blocks.append(
                {
                    "type": "input_text",
                    "text": f"[Anhang {name} konnte nicht gelesen werden: {meta.get('error')}]",
                }
            )
    return blocks


# ---------------------------------------------------------------------------
# Gesprächsverlauf und Systemprompt
# ---------------------------------------------------------------------------


def build_system_prompt(company: Company, *, username: str | None, role: str | None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    user_part = (
        f"Angemeldeter Benutzer: {username} (Rolle {role})."
        if username
        else "Zugriff über API-Token."
    )
    return (
        "Du bist der integrierte KI-Assistent von OpenBuchhaltung, einer "
        "deutschen Buchhaltungssoftware. Du hilfst bei Buchführung, Belegen, "
        "Auswertungen und Verwaltung und antwortest auf Deutsch.\n"
        f"Heutiges Datum: {today}. Aktive Gesellschaft: {company.name!r} "
        f"(company_id={company.id}, Währung {company.currency_code}). {user_part}\n"
        "Dir stehen die OpenBuchhaltung-Tools zur Verfügung. Verwende für "
        f"Tool-Aufrufe immer company_id={company.id}, sofern der Benutzer nicht "
        "ausdrücklich eine andere Gesellschaft nennt. Beträge sind Dezimalwerte "
        "mit Punkt als Dezimaltrenner, Datumsangaben im Format JJJJ-MM-TT.\n"
        "Führe schreibende Aktionen (Buchungen, Anlagen, Stammdaten) nur aus, "
        "wenn der Benutzer sie eindeutig angefordert hat, und fasse danach "
        "zusammen, was gebucht bzw. geändert wurde. Bei unklaren Aufträgen "
        "stelle zuerst eine Rückfrage."
    )


def _history_items(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            text = message.content
            for meta in message.attachments or []:
                if meta.get("kind") == "text" and meta.get("text"):
                    text += f"\n[Anhang {meta.get('file_name')}]\n{meta['text']}"
                elif meta.get("kind") == "image":
                    text += f"\n[Anhang (Bild): {meta.get('file_name')}]"
            items.append(
                {"role": "user", "content": [{"type": "input_text", "text": text}]}
            )
        elif message.role == "assistant":
            items.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message.content}],
                }
            )
    return items


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChatExchange:
    """Ergebnis eines Chat-Zuges für Web/API: Unterhaltung plus neue Nachrichten."""

    conversation_id: int
    conversation_title: str
    created_conversation: bool
    user_message: dict[str, Any]
    assistant_message: dict[str, Any]


def serialize_message(message: ChatMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "tool_calls": message.tool_calls or [],
        "attachments": [
            {key: value for key, value in meta.items() if key != "text"}
            for meta in (message.attachments or [])
        ],
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def accessible_conversation(
    session, conversation_id: int, *, tenant_id: int | None, user_id: int | None
) -> ChatConversation | None:
    """Lädt eine Unterhaltung im Zugriffsbereich (Tenant-Scope, eigener Benutzer)."""
    conversation = session.get(ChatConversation, conversation_id)
    if conversation is None:
        return None
    if tenant_id is not None and conversation.tenant_id != tenant_id:
        return None
    if user_id is not None and conversation.user_id != user_id:
        return None
    return conversation


def run_chat_message(
    *,
    session_factory,
    company: Company,
    conversation_id: int | None,
    message_text: str,
    uploads: list[tuple[str, bytes]],
    api_user: dict | None,
    global_access: bool,
) -> ChatExchange:
    """Führt einen kompletten Chat-Zug aus: persistieren, LLM-Schleife, Antwort.

    ``uploads`` ist eine Liste ``(dateiname, bytes)``. Wirft :class:`ChatError`
    bei fachlichen Fehlern und :class:`ChatLLMError` bei LLM-Problemen.
    """
    config = current_app.config
    endpoint_url = config.get("CHAT_LLM_ENDPOINT_URL")
    if not endpoint_url:
        raise ChatError(
            "Kein Chat-LLM konfiguriert. Bitte CHAT_LLM_ENDPOINT_URL (oder "
            "DOCUMENT_LLM_ENDPOINT_URL) setzen."
        )

    message_text = sanitize_text(message_text or "").strip()
    if not message_text and not uploads:
        raise ChatError("Die Nachricht ist leer.")

    attachments: list[dict[str, Any]] = []
    image_data: dict[str, bytes] = {}
    for file_name, data in uploads:
        meta = process_attachment(file_name=file_name, data=data)
        attachments.append(meta)
        if meta.get("kind") == "image":
            image_data[meta["file_name"]] = data

    user_id = api_user.get("id") if api_user else None
    tenant_scope = api_user.get("tenant_id") if api_user else None

    # 1) Unterhaltung laden/anlegen und Benutzer-Nachricht persistieren.
    with session_factory() as session:
        created_conversation = False
        if conversation_id is not None:
            conversation = accessible_conversation(
                session, conversation_id, tenant_id=tenant_scope, user_id=user_id
            )
            if conversation is None or conversation.company_id != company.id:
                raise ChatError("Unterhaltung nicht gefunden.")
        else:
            title_basis = message_text or (attachments[0]["file_name"] if attachments else "")
            title = title_basis[:MAX_TITLE_LENGTH] or "Neue Unterhaltung"
            conversation = ChatConversation(
                tenant_id=company.tenant_id,
                company_id=company.id,
                user_id=user_id,
                title=title,
            )
            session.add(conversation)
            session.flush()
            created_conversation = True

        history = list(conversation.messages)
        user_message = ChatMessage(
            conversation_id=conversation.id,
            role="user",
            content=message_text,
            attachments=attachments or None,
        )
        session.add(user_message)
        conversation.updated_at = datetime.now(timezone.utc)
        session.commit()

        conversation_id = conversation.id
        conversation_title = conversation.title
        user_message_data = serialize_message(user_message)
        history_items = _history_items(history)

    # 2) LLM-Schleife mit Tool-Ausführung (eigene DB-Sessions je Tool-Aufruf).
    system_prompt = build_system_prompt(
        company,
        username=api_user.get("username") if api_user else None,
        role=api_user.get("role") if api_user else None,
    )
    user_content: list[dict[str, Any]] = []
    if message_text:
        user_content.append({"type": "input_text", "text": message_text})
    user_content.extend(_attachment_content_blocks(attachments, image_data))

    input_items: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
        *history_items,
        {"role": "user", "content": user_content},
    ]

    execute_tool = build_tool_executor(api_user=api_user, global_access=global_access)
    try:
        turn = run_chat_turn(
            endpoint_url=endpoint_url,
            model=config.get("CHAT_LLM_MODEL") or "gpt-4.1-mini",
            input_items=input_items,
            tools=chat_tool_definitions(),
            execute_tool=execute_tool,
            api_key=config.get("CHAT_LLM_API_KEY"),
            max_tool_calls=int(config.get("CHAT_LLM_MAX_TOOL_CALLS") or 15),
            timeout=float(config.get("CHAT_LLM_TIMEOUT_SECONDS") or 120.0),
        )
    except ChatLLMError:
        # Benutzer-Nachricht bleibt gespeichert; Fehler geht an den Aufrufer.
        raise

    tool_call_log = []
    for call in turn.tool_calls:
        entry = call.to_dict()
        if len(entry["result_text"]) > TOOL_RESULT_STORE_LIMIT:
            entry["result_text"] = (
                entry["result_text"][:TOOL_RESULT_STORE_LIMIT] + "\n… [gekürzt]"
            )
        tool_call_log.append(entry)

    # 3) Antwort persistieren.
    with session_factory() as session:
        assistant_message = ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=sanitize_text(turn.reply_text),
            tool_calls=tool_call_log or None,
        )
        session.add(assistant_message)
        conversation = session.get(ChatConversation, conversation_id)
        if conversation is not None:
            conversation.updated_at = datetime.now(timezone.utc)
        session.commit()
        assistant_message_data = serialize_message(assistant_message)

    return ChatExchange(
        conversation_id=conversation_id,
        conversation_title=conversation_title,
        created_conversation=created_conversation,
        user_message=user_message_data,
        assistant_message=assistant_message_data,
    )
