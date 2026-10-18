"""UStVA und Zusammenfassende Meldung über die API: Kennziffern berechnen,
Voranmeldungen festhalten, ZM-Zeilen je Kunden-USt-IdNr. ermitteln."""

from __future__ import annotations

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    bool_arg,
    bool_payload,
    date_arg,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    compute_vat_return_details,
    list_vat_returns,
    period_bounds,
    save_vat_return,
    vat_return_kind_from_label,
)
from app.services.zm import compute_zm


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


def _period_args():
    """Zeitraum aus ``period`` oder ``date_from``/``date_to``.

    Liefert ``((Beginn, Ende, Label), None)`` oder ``(None, Fehlerantwort)``.
    """
    period_label = (request.args.get("period") or "").strip()
    try:
        if period_label:
            return period_bounds(period_label), None
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except (VatReturnError, DateArgError) as exc:
        return None, (jsonify({"error": str(exc)}), 400)
    if date_from is None or date_to is None:
        return None, (jsonify({"error": "period or date_from and date_to are required."}), 400)
    return (date_from, date_to, ""), None


@api_bp.get("/vat-return")
def get_vat_return():
    """Berechnet die UStVA-Kennziffern für einen Zeitraum (ohne zu speichern).

    Zeitraum entweder als ``period`` (JJJJ-MM, JJJJ-Qn, JJJJ-Hn oder JJJJ)
    oder als ``date_from``/``date_to``. ``warnings`` und ``unassigned`` nennen
    Erträge ohne Umsatzsteuer, die keiner Kennzahl zugeordnet sind, und
    § 13b-Steuer ohne Angabe zum leistenden Unternehmer.
    """
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    period, error = _period_args()
    if error is not None:
        return error
    date_from, date_to, period_label = period

    include_closing_entries = bool_arg("include_closing_entries")
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        result = compute_vat_return_details(
            session=session,
            company_id=company_id,
            date_from=date_from,
            date_to=date_to,
            include_closing_entries=include_closing_entries,
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
                "include_closing_entries": include_closing_entries,
                "kennzahlen": _rows_payload(result.rows),
                "warnings": result.warnings,
                "unassigned": result.unassigned,
            }
        ),
        200,
    )


@api_bp.get("/zm")
def get_zm_report():
    """Zusammenfassende Meldung: EU-Umsätze je Kunden-USt-IdNr. und Art (L/D/S).

    Zeitraum wie bei ``/vat-return``. ``missing`` listet ZM-relevante Erlöse ohne
    EU-USt-IdNr. des Kunden.
    """
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    period, error = _period_args()
    if error is not None:
        return error
    date_from, date_to, period_label = period

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        result = compute_zm(
            session=session, company_id=company_id, date_from=date_from, date_to=date_to
        )

    return jsonify(
        {
            "company_id": company_id,
            "period": period_label or None,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "rows": result.rows,
            "missing": result.missing,
            "warnings": result.warnings,
        }
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
                include_closing_entries=bool_payload(payload.get("include_closing_entries")),
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

    include_closing_entries = bool_arg("include_closing_entries")
    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        rows = compute_vat_return(
            session=session,
            company_id=company_id,
            date_from=date_from,
            date_to=date_to,
            include_closing_entries=include_closing_entries,
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
                "include_closing_entries": include_closing_entries,
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
                include_closing_entries=bool_payload(payload.get("include_closing_entries")),
            )
        except (TypeError, ValueError):
            return jsonify({"error": "year must be an integer."}), 400
        except VatReturnError as exc:
            return validation_error(str(exc))
        return jsonify(_vat_return_payload(vat_return)), 201
