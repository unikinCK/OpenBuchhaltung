"""Steuercodes über die API: auflisten, anlegen, Standard-Codes sicherstellen."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import Account, TaxCode


def _tax_code_dict(tax_code: TaxCode) -> dict[str, object]:
    return {
        "id": tax_code.id,
        "company_id": tax_code.company_id,
        "code": tax_code.code,
        "rate": str(tax_code.rate),
        "description": tax_code.description,
        "vat_account_id": tax_code.vat_account_id,
        "is_active": tax_code.is_active,
    }


@api_bp.get("/tax-codes")
def list_tax_codes_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        tax_codes = (
            session.execute(
                select(TaxCode)
                .where(TaxCode.company_id == company_id)
                .order_by(TaxCode.code)
            )
            .scalars()
            .all()
        )
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "tax_codes": [_tax_code_dict(tax_code) for tax_code in tax_codes],
                }
            ),
            200,
        )


@api_bp.post("/tax-codes")
def create_tax_code_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        code = (payload.get("code") or "").strip()
        rate = parse_decimal(str(payload.get("rate")))
    except (TypeError, ValueError, JournalEntryCreationError):
        return jsonify({"error": "company_id, code and a valid rate are required."}), 400
    if not code:
        return jsonify({"error": "company_id, code and a valid rate are required."}), 400
    if rate < 0:
        return validation_error("Der Steuersatz darf nicht negativ sein.")

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404

        vat_account_id = payload.get("vat_account_id")
        vat_account_code = (payload.get("vat_account_code") or "").strip()
        if vat_account_id is None and vat_account_code:
            vat_account_id = session.execute(
                select(Account.id).where(
                    Account.company_id == company.id, Account.code == vat_account_code
                )
            ).scalar_one_or_none()
            if vat_account_id is None:
                return validation_error(
                    f"Steuerkonto {vat_account_code} wurde nicht gefunden."
                )
        if vat_account_id is not None:
            account = session.get(Account, vat_account_id)
            if account is None or account.company_id != company.id:
                return validation_error("Steuerkonto gehört nicht zur Gesellschaft.")

        tax_code = TaxCode(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            rate=rate,
            description=(payload.get("description") or "").strip() or None,
            vat_account_id=vat_account_id,
        )
        session.add(tax_code)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Tax code already exists for this company."}), 409
        session.refresh(tax_code)
        return jsonify(_tax_code_dict(tax_code)), 201


@api_bp.post("/tax-codes/defaults")
def ensure_default_tax_codes_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        changed = ensure_default_tax_codes(session=session, company=company)
        session.commit()
        tax_codes = (
            session.execute(
                select(TaxCode)
                .where(TaxCode.company_id == company.id)
                .order_by(TaxCode.code)
            )
            .scalars()
            .all()
        )
        return (
            jsonify(
                {
                    "company_id": company.id,
                    "changed": changed,
                    "tax_codes": [_tax_code_dict(tax_code) for tax_code in tax_codes],
                }
            ),
            200,
        )
