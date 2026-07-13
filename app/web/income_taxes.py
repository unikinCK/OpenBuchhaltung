"""Ertragsteuern: KSt/GewSt-Arbeitsseite."""

from __future__ import annotations

from datetime import date

from flask import flash, redirect, render_template, request, url_for

from app.services.income_taxes import (
    DECLARATION_TYPE_DECLARATION,
    TAX_TYPE_CORPORATE_INCOME,
    IncomeTaxError,
    compute_income_tax_return,
    display_declaration_type,
    display_tax_type,
    list_income_tax_returns,
    save_income_tax_return,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)


def _form_adjustment(field: str, code: str, label: str) -> list[dict[str, str]]:
    amount = (request.values.get(field) or "0").strip()
    if amount in {"", "0", "0.00"}:
        return []
    return [{"code": code, "label": label, "amount": amount}]


def _request_params() -> dict[str, object]:
    additions_total = request.values.get("additions_total") or "0"
    reductions_total = request.values.get("reductions_total") or "0"
    return {
        "year": request.values.get("year") or str(date.today().year),
        "tax_type": request.values.get("tax_type") or TAX_TYPE_CORPORATE_INCOME,
        "declaration_type": request.values.get("declaration_type")
        or DECLARATION_TYPE_DECLARATION,
        "additions_total": additions_total,
        "reductions_total": reductions_total,
        "additions": _form_adjustment(
            "additions_total", "manual_additions", "Manuelle Hinzurechnungen"
        ),
        "reductions": _form_adjustment(
            "reductions_total", "manual_reductions", "Manuelle Kürzungen"
        ),
        "loss_carryforward": request.values.get("loss_carryforward") or "0",
        "prepayments": request.values.get("prepayments") or "0",
        "municipality_multiplier": request.values.get("municipality_multiplier") or None,
        "trade_tax_allowance": request.values.get("trade_tax_allowance") or "0",
    }


def _service_params(params: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in params.items()
        if key not in {"additions_total", "reductions_total"}
    }


@main_bp.get("/ertragsteuern")
def income_taxes_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        params = _request_params()
        preview = None
        error = None
        snapshots = []
        if selected_company_id:
            try:
                preview = compute_income_tax_return(
                    session=session,
                    company_id=selected_company_id,
                    **_service_params(params),
                )
            except IncomeTaxError as exc:
                error = str(exc)
            snapshots = list_income_tax_returns(
                session=session, company_id=selected_company_id
            )

    return render_template(
        "ertragsteuern.html",
        companies=companies,
        selected_company_id=selected_company_id,
        params=params,
        preview=preview,
        error=error,
        snapshots=snapshots,
        display_tax_type=display_tax_type,
        display_declaration_type=display_declaration_type,
    )


@main_bp.post("/ertragsteuern/festhalten")
def save_income_tax_return_action():
    company_id = request.form.get("company_id", type=int)
    params = _request_params()
    if not company_id:
        flash("Gesellschaft ist erforderlich.", "error")
        return redirect(url_for("main.income_taxes_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            item = save_income_tax_return(
                session=session,
                company_id=company_id,
                changed_by=changed_by(),
                **_service_params(params),
            )
        except IncomeTaxError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.income_taxes_page", company_id=company_id))

    flash(
        f"{display_tax_type(item.tax_type)} {display_declaration_type(item.declaration_type)} "
        f"{item.period_label} wurde festgehalten.",
        "success",
    )
    return redirect(
        url_for(
            "main.income_taxes_page",
            company_id=company_id,
            year=item.period_label,
            tax_type=item.tax_type,
            declaration_type=item.declaration_type,
        )
    )
