"""System-Endpunkte (Health-Check, ohne Auth)."""

from __future__ import annotations

from flask import jsonify

from app.api.blueprint import api_bp


@api_bp.get("/health")
def health():
    return jsonify({"status": "ok"}), 200
