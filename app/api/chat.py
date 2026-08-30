"""REST-API für den integrierten KI-Chat (/api/v1/chat/...)."""

from __future__ import annotations

import base64

from flask import jsonify, request
from sqlalchemy import select

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import api_has_global_access, current_api_tenant_id, current_api_user
from app.services.chat import (
    ChatError,
    accessible_conversation,
    run_chat_message,
    serialize_message,
)
from app.services.chat_llm import ChatLLMError
from domain.models import ChatConversation


def _conversation_payload(conversation: ChatConversation) -> dict:
    return {
        "id": conversation.id,
        "company_id": conversation.company_id,
        "user_id": conversation.user_id,
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat(),
        "updated_at": conversation.updated_at.isoformat(),
    }


def _api_user_filter():
    """Bei Benutzer-Token nur eigene Unterhaltungen; globales Token sieht alle."""
    user = current_api_user()
    return user["id"] if user else None


@api_bp.get("/chat/conversations")
def list_chat_conversations():
    company_id = request.args.get("company_id", type=int)
    if company_id is None:
        return validation_error("company_id ist erforderlich.")
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        statement = select(ChatConversation).where(
            ChatConversation.company_id == company_id
        )
        user_id = _api_user_filter()
        if user_id is not None:
            statement = statement.where(ChatConversation.user_id == user_id)
        rows = (
            session.execute(statement.order_by(ChatConversation.updated_at.desc()))
            .scalars()
            .all()
        )
        return jsonify({"conversations": [_conversation_payload(row) for row in rows]})


@api_bp.get("/chat/conversations/<int:conversation_id>")
def get_chat_conversation(conversation_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        conversation = accessible_conversation(
            session,
            conversation_id,
            tenant_id=current_api_tenant_id(),
            user_id=_api_user_filter(),
        )
        if conversation is None:
            return jsonify({"error": "Conversation not found."}), 404
        payload = _conversation_payload(conversation)
        payload["messages"] = [
            serialize_message(message) for message in conversation.messages
        ]
        return jsonify(payload)


@api_bp.post("/chat/conversations/<int:conversation_id>/delete")
def delete_chat_conversation(conversation_id: int):
    if not api_can_write():
        return forbidden()
    session_factory = get_session_factory()
    with session_factory() as session:
        conversation = accessible_conversation(
            session,
            conversation_id,
            tenant_id=current_api_tenant_id(),
            user_id=_api_user_filter(),
        )
        if conversation is None:
            return jsonify({"error": "Conversation not found."}), 404
        session.delete(conversation)
        session.commit()
    return jsonify({"status": "deleted", "conversation_id": conversation_id})


@api_bp.post("/chat/messages")
def send_chat_message():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    if not isinstance(company_id, int):
        return validation_error("company_id ist erforderlich.")
    message_text = payload.get("message")
    if message_text is not None and not isinstance(message_text, str):
        return validation_error("message muss ein String sein.")
    conversation_id = payload.get("conversation_id")
    if conversation_id is not None and not isinstance(conversation_id, int):
        return validation_error("conversation_id muss eine Zahl sein.")

    uploads: list[tuple[str, bytes]] = []
    raw_attachments = payload.get("attachments") or []
    if not isinstance(raw_attachments, list):
        return validation_error("attachments muss eine Liste sein.")
    for entry in raw_attachments:
        if not isinstance(entry, dict) or not entry.get("file_name"):
            return validation_error(
                "Jeder Anhang benötigt file_name und content_base64."
            )
        try:
            data = base64.b64decode(entry.get("content_base64") or "", validate=True)
        except (ValueError, TypeError):
            return validation_error(
                f"content_base64 von {entry['file_name']!r} ist ungültig."
            )
        uploads.append((entry["file_name"], data))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        session.expunge(company)

    try:
        exchange = run_chat_message(
            session_factory=session_factory,
            company=company,
            conversation_id=conversation_id,
            message_text=message_text or "",
            uploads=uploads,
            api_user=current_api_user(),
            global_access=api_has_global_access(),
        )
    except ChatError as exc:
        return validation_error(str(exc))
    except ChatLLMError as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(
        {
            "conversation_id": exchange.conversation_id,
            "conversation_title": exchange.conversation_title,
            "created_conversation": exchange.created_conversation,
            "user_message": exchange.user_message,
            "assistant_message": exchange.assistant_message,
        }
    )
