"""Geschäftspartner (Debitoren/Kreditoren) über die REST-API."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import api_can_write, api_scoped_company, forbidden, get_session_factory
from app.auth import current_api_user
from app.services.partners import (
    UPDATABLE_FIELDS,
    PartnerError,
    create_partner,
    duplicate_hints,
    list_partners,
    partner_history,
    serialize_partner,
    set_partner_bank_details,
    update_partner,
)
from domain.models import SUBLEDGER_TYPES, BusinessPartner

CREATE_FIELDS = {"company_id", *UPDATABLE_FIELDS}
BANK_FIELDS = {"iban", "bic"}


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _scoped_partner(session, partner_id: int) -> BusinessPartner | None:
    partner = session.get(BusinessPartner, partner_id)
    if partner is None or api_scoped_company(session, partner.company_id) is None:
        return None
    return partner


def _bank_fields_error():
    return (
        jsonify(
            {
                "error": "Bankdaten (iban/bic) werden nur über "
                "POST /partners/<id>/bank-details gesetzt."
            }
        ),
        400,
    )


@api_bp.get("/partners")
def list_partners_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    role = (request.args.get("role") or "").strip() or None
    if role is not None and role not in SUBLEDGER_TYPES:
        return jsonify({"error": "role must be debtor or creditor."}), 400
    include_inactive = request.args.get("include_inactive", "").lower() in {"1", "true", "yes"}
    query = (request.args.get("q") or "").strip() or None
    limit = request.args.get("limit", default=100, type=int)
    offset = request.args.get("offset", default=0, type=int)
    if limit is None or offset is None:
        return jsonify({"error": "limit and offset must be integers."}), 400
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        partners, total = list_partners(
            session=session,
            company_id=company_id,
            role=role,
            query=query,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
        return jsonify(
            {
                "company_id": company_id,
                "total": total,
                "limit": limit,
                "offset": offset,
                "partners": [serialize_partner(partner) for partner in partners],
            }
        )


@api_bp.post("/partners")
def create_partner_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    if BANK_FIELDS & set(payload):
        return _bank_fields_error()
    unknown = set(payload) - CREATE_FIELDS
    if unknown:
        return jsonify({"error": f"Unknown fields: {', '.join(sorted(unknown))}."}), 400
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        fields: dict[str, Any] = {
            key: value for key, value in payload.items() if key != "company_id"
        }
        try:
            partner = create_partner(
                session=session,
                company=company,
                changed_by=_api_changed_by(),
                **fields,
            )
        except (PartnerError, TypeError) as exc:
            session.rollback()
            return jsonify({"error": str(exc) or "Invalid payload."}), 400
        warnings = duplicate_hints(
            session=session,
            company_id=company.id,
            name=partner.name,
            vat_id=partner.vat_id,
            exclude_id=partner.id,
        )
        session.commit()
        response = serialize_partner(partner)
        response["warnings"] = warnings
        return jsonify(response), 201


@api_bp.get("/partners/<int:partner_id>")
def get_partner_via_api(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _scoped_partner(session, partner_id)
        if partner is None:
            return jsonify({"error": "Partner not found."}), 404
        return jsonify(serialize_partner(partner))


@api_bp.patch("/partners/<int:partner_id>")
def update_partner_via_api(partner_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    if BANK_FIELDS & set(payload):
        return _bank_fields_error()
    unknown = set(payload) - set(UPDATABLE_FIELDS)
    if unknown:
        message = f"Unknown or immutable fields: {', '.join(sorted(unknown))}."
        return jsonify({"error": message}), 400
    if not payload:
        return jsonify({"error": "At least one editable field is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _scoped_partner(session, partner_id)
        if partner is None:
            return jsonify({"error": "Partner not found."}), 404
        try:
            changed = update_partner(
                session=session,
                partner=partner,
                changed_by=_api_changed_by(),
                **payload,
            )
        except PartnerError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 400
        warnings = duplicate_hints(
            session=session,
            company_id=partner.company_id,
            name=partner.name,
            vat_id=partner.vat_id,
            exclude_id=partner.id,
        )
        session.commit()
        response = serialize_partner(partner)
        response["changed"] = changed
        response["warnings"] = warnings
        return jsonify(response)


@api_bp.post("/partners/<int:partner_id>/bank-details")
def set_partner_bank_details_via_api(partner_id: int):
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - BANK_FIELDS
    if unknown:
        return jsonify({"error": f"Unknown fields: {', '.join(sorted(unknown))}."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _scoped_partner(session, partner_id)
        if partner is None:
            return jsonify({"error": "Partner not found."}), 404
        try:
            changed = set_partner_bank_details(
                session=session,
                partner=partner,
                changed_by=_api_changed_by(),
                iban=payload.get("iban"),
                bic=payload.get("bic"),
            )
        except PartnerError as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 400
        warnings = (
            duplicate_hints(
                session=session,
                company_id=partner.company_id,
                iban=partner.iban,
                exclude_id=partner.id,
            )
            if partner.iban
            else []
        )
        session.commit()
        response = serialize_partner(partner)
        response["changed"] = changed
        response["warnings"] = warnings
        return jsonify(response)


@api_bp.get("/partners/<int:partner_id>/history")
def get_partner_history_via_api(partner_id: int):
    limit = request.args.get("limit", default=100, type=int)
    if limit is None:
        return jsonify({"error": "limit must be an integer."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _scoped_partner(session, partner_id)
        if partner is None:
            return jsonify({"error": "Partner not found."}), 404
        return jsonify(
            {
                "partner": serialize_partner(partner),
                "entries": partner_history(session=session, partner_id=partner.id, limit=limit),
                "limit": max(1, min(limit, 500)),
            }
        )
