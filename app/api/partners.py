"""Geschäftspartner (Debitoren/Kreditoren) über die REST-API."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any

from flask import Response, jsonify, request

from app.api.blueprint import api_bp
from app.api.downloads import linkable_download
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.partner_ledger import (
    DEFAULT_STATEMENT_LIMIT,
    PartnerLedgerError,
    partner_balances,
    partner_statement,
    serialize_partner_balances,
    serialize_statement_side,
)
from app.services.partner_recognition import PartnerSignals, recognize_partner
from app.services.partner_takeover import (
    PartnerTakeoverError,
    apply_partner_takeover,
    partner_takeover_proposals,
    serialize_takeover_proposal,
)
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
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    SUBLEDGER_TYPES,
    BusinessPartner,
)

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


_RECOGNIZE_ROLES = {
    "customer": SUBLEDGER_DEBTOR,
    "debtor": SUBLEDGER_DEBTOR,
    "supplier": SUBLEDGER_CREDITOR,
    "creditor": SUBLEDGER_CREDITOR,
}


@api_bp.post("/partners/recognize")
def recognize_partner_via_api():
    """Erkennt einen Partner aus Kennungen; ändert nichts und ordnet nichts zu."""
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400
    role_name = str(payload.get("role") or "").strip().lower()
    if role_name and role_name not in _RECOGNIZE_ROLES:
        return jsonify({"error": "role must be customer or supplier."}), 400

    def values(name: str) -> tuple[str, ...]:
        value = str(payload.get(name) or "").strip()
        return (value,) if value else ()

    signals = PartnerSignals(
        vat_ids=values("vat_id"),
        tax_numbers=values("tax_number"),
        ibans=values("iban"),
        partner_numbers=values("partner_number"),
        name=str(payload.get("name") or "").strip() or None,
    )
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        match = recognize_partner(
            session, company_id, signals, role=_RECOGNIZE_ROLES.get(role_name)
        )
        return jsonify(match.as_dict())


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


@api_bp.get("/partners/<int:partner_id>/statement")
def get_partner_statement_via_api(partner_id: int):
    limit = request.args.get("limit", default=DEFAULT_STATEMENT_LIMIT, type=int)
    offset = request.args.get("offset", default=0, type=int)
    if limit is None or offset is None:
        return jsonify({"error": "limit and offset must be integers."}), 400
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _scoped_partner(session, partner_id)
        if partner is None:
            return jsonify({"error": "Partner not found."}), 404
        try:
            statement = partner_statement(
                session=session,
                partner=partner,
                side=(request.args.get("side") or "").strip() or None,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            )
        except PartnerLedgerError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "partner": serialize_partner(partner),
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "limit": statement["limit"],
                "offset": statement["offset"],
                "sides": [serialize_statement_side(side) for side in statement["sides"]],
                "open_items": [
                    {
                        "id": item.id,
                        "item_type": item.item_type,
                        "reference": item.reference,
                        "entry_date": item.entry_date.isoformat(),
                        "due_date": item.due_date.isoformat() if item.due_date else None,
                        "original_amount": str(item.original_amount),
                        "open_amount": str(item.open_amount),
                        "currency_code": item.currency_code,
                        "dunning_level": item.dunning_level,
                    }
                    for item in statement["open_items"]
                ],
            }
        )


@api_bp.get("/partners/takeover-proposals")
def list_partner_takeover_proposals_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        proposals = partner_takeover_proposals(session=session, company_id=company_id)
    return jsonify(
        {
            "company_id": company_id,
            "proposals": [serialize_takeover_proposal(proposal) for proposal in proposals],
        }
    )


@api_bp.post("/partners/takeover")
def apply_partner_takeover_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        partner_id = (
            int(payload["partner_id"]) if payload.get("partner_id") not in (None, "") else None
        )
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and partner_id must be integers."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = apply_partner_takeover(
                session=session,
                company_id=company_id,
                key=str(payload.get("key") or ""),
                partner_id=partner_id,
                changed_by=_api_changed_by(),
            )
        except PartnerTakeoverError as exc:
            return jsonify({"error": str(exc)}), 422
    return jsonify(result), 200


def _partner_balances_report(session, company_id: int):
    return partner_balances(
        session=session,
        company_id=company_id,
        side=(request.args.get("side") or SUBLEDGER_DEBTOR).strip(),
        as_of=date_arg("as_of"),
    )


@api_bp.get("/reports/partner-balances")
def get_partner_balances_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            report = _partner_balances_report(session, company_id)
        except (DateArgError, PartnerLedgerError) as exc:
            return jsonify({"error": str(exc)}), 400
    return jsonify(serialize_partner_balances(report))


@api_bp.get("/exports/partner-balances.csv")
@linkable_download
def export_partner_balances_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            report = _partner_balances_report(session, company_id)
        except (DateArgError, PartnerLedgerError) as exc:
            return jsonify({"error": str(exc)}), 400
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "number",
            "name",
            "debit_total",
            "credit_total",
            "balance",
            "open_items_total",
            "opos_difference",
        ]
    )
    for row in report["partners"]:
        writer.writerow(
            [
                row["number"],
                row["name"],
                row["debit_total"],
                row["credit_total"],
                row["balance"],
                row["open_items_total"],
                row["opos_difference"],
            ]
        )
    file_name = (
        f"saldenliste-{report['side']}-{company_id}-{report['as_of'].isoformat()}.csv"
    )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )
