from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import current_app, flash, jsonify, redirect, request, session, url_for


def current_user_role() -> str | None:
    role_from_header = request.headers.get("X-User-Role")
    if role_from_header:
        return role_from_header.strip().lower()

    user = session.get("user") or {}
    role = user.get("role")
    if isinstance(role, str):
        return role.strip().lower()
    default_role = current_app.config.get("DEFAULT_USER_ROLE")
    if isinstance(default_role, str) and default_role.strip():
        return default_role.strip().lower()
    return None


def current_user_name() -> str:
    user_from_header = request.headers.get("X-User-Name")
    if user_from_header:
        return user_from_header.strip()

    user = session.get("user") or {}
    username = user.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    default_user = current_app.config.get("DEFAULT_USER_NAME")
    if isinstance(default_user, str) and default_user.strip():
        return default_user.strip()
    return "anonymous"


def require_roles(*allowed_roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    allowed = {role.strip().lower() for role in allowed_roles}

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any):
            role = current_user_role()
            if role not in allowed:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Forbidden: insufficient role."}), 403
                flash("Keine Berechtigung für diese Aktion.", "error")
                company_id = request.form.get("company_id", type=int) or request.args.get(
                    "company_id", type=int
                )
                return redirect(url_for("main.index", company_id=company_id))
            return func(*args, **kwargs)

        return wrapper

    return decorator
