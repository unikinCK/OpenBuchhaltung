"""UStVA über die API: Kennziffern berechnen und Voranmeldungen festhalten."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    list_vat_returns,
    period_bounds,
    save_vat_return,
    vat_return_kind_from_label,
)


def _rows_payload(rows) -> list[dict[str, str]]:
    return [
        {"kennziffer": row.kennziffer, "label": row.label, "amount": str(row.amount)}
        for row in rows
    ]


def _vat_return_payload(vat_return) -> dict[str, object]:
    return {
        "id": vat_return.id,
        "period_label": vat_return.period_label,
        "declaration_type": vat_return_kind_from_label(vat_return.period_label),
        "date_from": vat_return.date_from.isoformat(),
        "date_to": vat_return.date_to.isoformat(),
        "status": vat_return.status,
        "kennzahlen": vat_return.kennzahlen,
    }


@api_bp.get("/vat-return")
def get_vat_return():
    """Berechnet die UStVA-Kennziffern für einen Zeitraum (ohne zu speichern).

    Zeitraum entweder als ``period`` (JJJJ-MM, JJJJ-Qn, JJJJ-Hn oder JJJJ)
    oder als ``date_from``/``date_to``.
    """
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    period_label = (request.args.get("period") or "").strip()
    try:
        if period_label:
            date_from, date_to, period_label = period_bounds(period_label)
        else:
            date_from = date_arg("date_from")
            date_to = date_arg("date_to")
            if date_from is None or date_to is None:
                return jsonify(
                    {"error": "period or date_from and date_to are required."}
                ), 400
    except VatReturnError as exc:
        return jsonify({"error": str(exc)}), 400
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        rows = compute_vat_return(
            session=session, company_id=company_id, date_from=date_from, date_to=date_to
        )

    return (
        jsonify(
            {
                "company_id": company_id,
                "period": period_label or None,
                "declaration_type": (
                    vat_return_kind_from_label(period_label) if period_label else "custom"
                ),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "kennzahlen": _rows_payload(rows),
            }
        ),
        200,
    )


@api_bp.get("/vat-returns")
def list_vat_returns_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        items = list_vat_returns(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "vat_returns": [
                        {
                            "id": item.id,
                            "period_label": item.period_label,
                            "declaration_type": vat_return_kind_from_label(
                                item.period_label
                            ),
                            "date_from": item.date_from.isoformat(),
                            "date_to": item.date_to.isoformat(),
                            "status": item.status,
                            "kennzahlen": item.kennzahlen,
                            "created_at": item.created_at.isoformat(),
                            "created_by": item.created_by,
                        }
                        for item in items
                    ],
                }
            ),
            200,
        )


@api_bp.post("/vat-returns")
def create_vat_return_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    period_label = (payload.get("period") or "").strip()
    if not company_id or not period_label:
        return jsonify({"error": "company_id and period are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, int(company_id)) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            vat_return = save_vat_return(
                session=session,
                company_id=int(company_id),
                period_label=period_label,
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except VatReturnError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                _vat_return_payload(vat_return)
            ),
            201,
        )


@api_bp.get("/vat-annual-return")
def get_vat_annual_return():
    company_id = request.args.get("company_id", type=int)
    year = request.args.get("year", type=int)
    if not company_id or not year:
        return jsonify({"error": "company_id and year are required."}), 400

    try:
        date_from, date_to, period_label = period_bounds(str(year))
    except VatReturnError as exc:
        return jsonify({"error": str(exc)}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        rows = compute_vat_return(
            session=session, company_id=company_id, date_from=date_from, date_to=date_to
        )

    return (
        jsonify(
            {
                "company_id": company_id,
                "year": year,
                "period": period_label,
                "declaration_type": "annual",
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "kennzahlen": _rows_payload(rows),
            }
        ),
        200,
    )


@api_bp.post("/vat-annual-returns")
def create_vat_annual_return_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    company_id = payload.get("company_id")
    year = payload.get("year")
    if not company_id or not year:
        return jsonify({"error": "company_id and year are required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, int(company_id)) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            vat_return = save_vat_return(
                session=session,
                company_id=int(company_id),
                period_label=str(int(year)),
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except (TypeError, ValueError):
            return jsonify({"error": "year must be an integer."}), 400
        except VatReturnError as exc:
            return validation_error(str(exc))
        return jsonify(_vat_return_payload(vat_return)), 201
