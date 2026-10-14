"""Öffentliche Demo-Instanz (``DEMO_MODE``): Banner, Login-Hinweis, gesperrte Bereiche."""

from __future__ import annotations

from flask import Flask, flash, jsonify, redirect, request

from app.services.demo import DEMO_LOGIN_PASSWORD, DEMO_LOGIN_USERNAME, demo_block_reason


def register_demo_mode(app: Flask) -> None:
    @app.context_processor
    def _inject_demo_mode():
        if not app.config.get("DEMO_MODE"):
            return {"demo_mode": False}
        return {
            "demo_mode": True,
            "demo_login": {"username": DEMO_LOGIN_USERNAME, "password": DEMO_LOGIN_PASSWORD},
            "demo_reset_time": app.config.get("DEMO_RESET_TIME"),
        }

    @app.before_request
    def _block_in_demo_mode():
        if not app.config.get("DEMO_MODE"):
            return None
        reason = demo_block_reason(request.path, request.method)
        if reason is None:
            return None
        message = f"In der Demo-Instanz gesperrt: {reason}."
        if request.path.startswith("/api/"):
            return jsonify({"error": message, "demo": True}), 403
        flash(message, "error")
        return redirect(request.referrer or "/")
