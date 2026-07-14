"""Anlagenbuchhaltung über die API."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import jsonify, request
from sqlalchemy.exc import IntegrityError

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
    validation_error,
)
from app.auth import current_api_user
from app.services.fixed_assets import (
    FixedAssetError,
    FixedAssetInput,
    create_fixed_asset,
    current_book_value,
    depreciation_schedule,
    dispose_fixed_asset,
    list_fixed_assets,
    post_depreciation,
    record_impairment,
)
from app.services.journal_entries import JournalEntryCreationError, parse_decimal
from domain.models import FixedAsset


def _fixed_asset_dict(session, asset) -> dict[str, object]:
    return {
        "id": asset.id,
        "company_id": asset.company_id,
        "asset_number": asset.asset_number,
        "name": asset.name,
        "method": asset.method,
        "acquisition_date": asset.acquisition_date.isoformat(),
        "in_service_date": asset.in_service_date.isoformat(),
        "acquisition_cost": str(asset.acquisition_cost),
        "residual_value": str(asset.residual_value),
        "useful_life_months": asset.useful_life_months,
        "degressive_rate": str(asset.degressive_rate) if asset.degressive_rate else None,
        "status": asset.status,
        "book_value": str(current_book_value(session=session, asset=asset)),
        "asset_account_id": asset.asset_account_id,
        "depreciation_account_id": asset.depreciation_account_id,
        "cost_center_id": asset.cost_center_id,
        "profit_center_id": asset.profit_center_id,
    }


@api_bp.post("/fixed-assets")
def create_fixed_asset_via_api():
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id"))
        acquisition_date = date.fromisoformat(payload.get("acquisition_date"))
    except (TypeError, ValueError):
        return jsonify({"error": "company_id and valid acquisition_date are required."}), 400

    in_service_raw = payload.get("in_service_date")
    useful_life = payload.get("useful_life_months")
    try:
        asset_input = FixedAssetInput(
            company_id=company_id,
            asset_number=(payload.get("asset_number") or "").strip(),
            name=(payload.get("name") or "").strip(),
            acquisition_date=acquisition_date,
            in_service_date=date.fromisoformat(in_service_raw) if in_service_raw else None,
            acquisition_cost=parse_decimal(str(payload.get("acquisition_cost"))),
            method=(payload.get("method") or "").strip(),
            useful_life_months=int(useful_life) if useful_life is not None else None,
            degressive_rate=(
                parse_decimal(str(payload["degressive_rate"]))
                if payload.get("degressive_rate") is not None
                else None
            ),
            total_units=(
                parse_decimal(str(payload["total_units"]))
                if payload.get("total_units") is not None
                else None
            ),
            residual_value=(
                parse_decimal(str(payload["residual_value"]))
                if payload.get("residual_value") is not None
                else Decimal("0.00")
            ),
            keep_memo_value=bool(payload.get("keep_memo_value", False)),
            notes=payload.get("notes"),
            asset_account_id=payload.get("asset_account_id"),
            asset_account_code=payload.get("asset_account_code"),
            depreciation_account_id=payload.get("depreciation_account_id"),
            depreciation_account_code=payload.get("depreciation_account_code"),
            cost_center_id=payload.get("cost_center_id"),
            profit_center_id=payload.get("profit_center_id"),
            changed_by=(current_api_user() or {}).get("username", "api"),
        )
    except (JournalEntryCreationError, ValueError) as exc:
        return validation_error(str(exc))

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            asset = create_fixed_asset(session=session, payload=asset_input)
        except FixedAssetError as exc:
            return validation_error(str(exc))
        except IntegrityError:
            session.rollback()
            return jsonify({"error": "Asset number already exists for this company."}), 409
        return jsonify(_fixed_asset_dict(session, asset)), 201


@api_bp.get("/fixed-assets")
def list_fixed_assets_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        assets = list_fixed_assets(session=session, company_id=company_id)
        return (
            jsonify(
                {
                    "company_id": company_id,
                    "assets": [_fixed_asset_dict(session, asset) for asset in assets],
                }
            ),
            200,
        )


@api_bp.get("/fixed-assets/<int:asset_id>/schedule")
def fixed_asset_schedule_via_api(asset_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None or api_scoped_company(session, asset.company_id) is None:
            return jsonify({"error": "Fixed asset not found."}), 404
        try:
            rows = depreciation_schedule(asset)
        except ValueError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "asset_id": asset.id,
                    "method": asset.method,
                    "schedule": [
                        {
                            "year": row.year,
                            "months": row.months,
                            "book_value_start": str(row.book_value_start),
                            "depreciation": str(row.depreciation),
                            "book_value_end": str(row.book_value_end),
                            "cumulative": str(row.cumulative),
                            "note": row.note,
                        }
                        for row in rows
                    ],
                }
            ),
            200,
        )


@api_bp.post("/fixed-assets/<int:asset_id>/depreciation")
def post_fixed_asset_depreciation_via_api(asset_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    fiscal_year = payload.get("fiscal_year")
    if not fiscal_year:
        return jsonify({"error": "fiscal_year is required."}), 400

    units = payload.get("units")
    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None or api_scoped_company(session, asset.company_id) is None:
            return jsonify({"error": "Fixed asset not found."}), 404
        try:
            entry = post_depreciation(
                session=session,
                fixed_asset_id=asset_id,
                fiscal_year=int(fiscal_year),
                units=parse_decimal(str(units)) if units is not None else None,
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except FixedAssetError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "id": entry.id,
                    "fixed_asset_id": entry.fixed_asset_id,
                    "fiscal_year": entry.fiscal_year,
                    "amount": str(entry.amount),
                    "book_value_before": str(entry.book_value_before),
                    "book_value_after": str(entry.book_value_after),
                    "journal_entry_id": entry.journal_entry_id,
                }
            ),
            201,
        )


@api_bp.post("/fixed-assets/<int:asset_id>/impairment")
def impair_fixed_asset_via_api(asset_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        fiscal_year = int(payload.get("fiscal_year") or date.today().year)
        amount = parse_decimal(str(payload.get("amount")))
        depreciation_date_raw = payload.get("depreciation_date")
        depreciation_date = (
            date.fromisoformat(depreciation_date_raw) if depreciation_date_raw else None
        )
    except (TypeError, ValueError):
        return (
            jsonify({"error": "valid fiscal_year, amount and depreciation_date are required."}),
            400,
        )

    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None or api_scoped_company(session, asset.company_id) is None:
            return jsonify({"error": "Fixed asset not found."}), 404
        try:
            entry = record_impairment(
                session=session,
                fixed_asset_id=asset_id,
                fiscal_year=fiscal_year,
                amount=amount,
                depreciation_date=depreciation_date,
                note=payload.get("note"),
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except FixedAssetError as exc:
            return validation_error(str(exc))
        return (
            jsonify(
                {
                    "id": entry.id,
                    "fixed_asset_id": entry.fixed_asset_id,
                    "fiscal_year": entry.fiscal_year,
                    "amount": str(entry.amount),
                    "book_value_before": str(entry.book_value_before),
                    "book_value_after": str(entry.book_value_after),
                    "journal_entry_id": entry.journal_entry_id,
                    "kind": entry.kind,
                }
            ),
            201,
        )


@api_bp.post("/fixed-assets/<int:asset_id>/disposal")
def dispose_fixed_asset_via_api(asset_id: int):
    if not api_can_write():
        return forbidden()

    payload = request.get_json(silent=True) or {}
    try:
        disposal_date = date.fromisoformat(payload.get("disposal_date") or date.today().isoformat())
        proceeds = (
            parse_decimal(str(payload["proceeds"])) if payload.get("proceeds") is not None else None
        )
        fiscal_year = (
            int(payload["fiscal_year"]) if payload.get("fiscal_year") is not None else None
        )
    except (TypeError, ValueError):
        return (
            jsonify({"error": "valid disposal_date, proceeds and fiscal_year are required."}),
            400,
        )

    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None or api_scoped_company(session, asset.company_id) is None:
            return jsonify({"error": "Fixed asset not found."}), 404
        try:
            asset = dispose_fixed_asset(
                session=session,
                fixed_asset_id=asset_id,
                disposal_date=disposal_date,
                proceeds=proceeds,
                fiscal_year=fiscal_year,
                changed_by=(current_api_user() or {}).get("username", "api"),
            )
        except FixedAssetError as exc:
            return validation_error(str(exc))
        return jsonify(_fixed_asset_dict(session, asset)), 200
