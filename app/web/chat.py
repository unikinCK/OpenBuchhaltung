"""UI-Routen für den integrierten KI-Chat."""

from __future__ import annotations

from flask import jsonify, redirect, render_template, request, url_for
from sqlalchemy import select

from app.auth import current_user
from app.services.chat import (
    ChatError,
    accessible_conversation,
    run_chat_message,
    serialize_message,
)
from app.services.chat_llm import ChatLLMError
from app.web.blueprint import main_bp
from app.web.helpers import (
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import ChatConversation


@main_bp.get("/chat")
def chat_page():
    session_factory = get_session_factory()
    user = current_user()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        conversations: list[dict] = []
        selected_conversation = None
        messages: list[dict] = []
        if selected_company_id is not None:
            rows = (
                session.execute(
                    select(ChatConversation)
                    .where(
                        ChatConversation.company_id == selected_company_id,
                        ChatConversation.user_id == user["id"],
                    )
                    .order_by(ChatConversation.updated_at.desc())
                )
                .scalars()
                .all()
            )
            conversations = [
                {"id": row.id, "title": row.title, "updated_at": row.updated_at}
                for row in rows
            ]

            conversation_id = request.args.get("conversation_id", type=int)
            if conversation_id is not None:
                conversation = accessible_conversation(
                    session,
                    conversation_id,
                    tenant_id=user.get("tenant_id"),
                    user_id=user["id"],
                )
                if conversation is not None and conversation.company_id == selected_company_id:
                    selected_conversation = {
                        "id": conversation.id,
                        "title": conversation.title,
                    }
                    messages = [
                        serialize_message(message) for message in conversation.messages
                    ]

    return render_template(
        "chat.html",
        companies=companies,
        selected_company_id=selected_company_id,
        conversations=conversations,
        selected_conversation=selected_conversation,
        messages=messages,
    )


@main_bp.post("/chat/send")
def chat_send():
    session_factory = get_session_factory()
    user = current_user()
    company_id = request.form.get("company_id", type=int)
    if company_id is None:
        return jsonify({"error": "company_id fehlt."}), 400

    with session_factory() as session:
        company = require_company_access(session, company_id)
        # Company-Objekt außerhalb der Session weiterverwenden (nur Wertzugriffe).
        session.expunge(company)

    conversation_id = request.form.get("conversation_id", type=int)
    message_text = request.form.get("message", "")
    uploads = [
        (upload.filename, upload.read())
        for upload in request.files.getlist("attachments")
        if upload and upload.filename
    ]

    try:
        exchange = run_chat_message(
            session_factory=session_factory,
            company=company,
            conversation_id=conversation_id,
            message_text=message_text,
            uploads=uploads,
            api_user=user,
            global_access=user.get("tenant_id") is None,
        )
    except ChatError as exc:
        return jsonify({"error": str(exc)}), 400
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


@main_bp.post("/chat/<int:conversation_id>/delete")
def chat_delete(conversation_id: int):
    session_factory = get_session_factory()
    user = current_user()
    with session_factory() as session:
        conversation = accessible_conversation(
            session, conversation_id, tenant_id=user.get("tenant_id"), user_id=user["id"]
        )
        company_id = conversation.company_id if conversation else None
        if conversation is not None:
            session.delete(conversation)
            session.commit()
    return redirect(url_for("main.chat_page", company_id=company_id))
