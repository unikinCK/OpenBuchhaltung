"""Anlagenbuchhaltung: Anlagenverzeichnis, AfA-, Abwertungs- und Abgangsbuchungen."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError

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
from app.services.journal_entries import parse_decimal
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    optional_decimal,
    require_company_access,
    safe_optional_decimal,
)
from domain.models import Account, FixedAsset
from domain.services import depreciation as afa


@main_bp.get("/anlagen")
def fixed_assets_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        accounts = []
        assets = []
        asset_views = []
        selected_asset = None
        schedule = []
        total_book_value = Decimal("0.00")
        if selected_company_id:
            accounts = (
                session.execute(
                    scoped_select(Account, company_id=selected_company_id)
                    .where(Account.is_active.is_(True))
                    .order_by(Account.code)
                )
                .scalars()
                .all()
            )
            assets = list_fixed_assets(session=session, company_id=selected_company_id)
            for asset in assets:
                book_value = current_book_value(session=session, asset=asset)
                if asset.status != "disposed":
                    total_book_value += book_value
                asset_views.append({"asset": asset, "book_value": book_value})

            selected_asset_id = request.args.get("asset_id", type=int)
            if selected_asset_id:
                selected_asset = next(
                    (a for a in assets if a.id == selected_asset_id), None
                )
                if selected_asset is not None:
                    try:
                        schedule = depreciation_schedule(selected_asset)
                    except afa.DepreciationError:
                        schedule = []

    return render_template(
        "anlagen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        asset_views=asset_views,
        selected_asset=selected_asset,
        schedule=schedule,
        methods=sorted(afa.METHODS),
        total_book_value=total_book_value,
        current_year=date.today().year,
        today=date.today().isoformat(),
    )


@main_bp.post("/anlagen")
def create_fixed_asset_action():
    company_id = request.form.get("company_id", type=int)
    asset_account_id = request.form.get("asset_account_id", type=int)
    depreciation_account_id = request.form.get("depreciation_account_id", type=int)
    method = request.form.get("method", "").strip()
    asset_number = request.form.get("asset_number", "").strip()
    name = request.form.get("name", "").strip()

    if not company_id or not asset_account_id or not depreciation_account_id or not method:
        flash("Gesellschaft, Verfahren, Anlage- und Abschreibungskonto sind Pflicht.", "error")
        return redirect(url_for("main.fixed_assets_page", company_id=company_id))

    try:
        acquisition_date = date.fromisoformat(request.form.get("acquisition_date", "").strip())
        in_service_raw = request.form.get("in_service_date", "").strip()
        in_service_date = date.fromisoformat(in_service_raw) if in_service_raw else None
        acquisition_cost = parse_decimal(request.form.get("acquisition_cost", "").strip())
        useful_life_raw = request.form.get("useful_life_months", "").strip()
        useful_life_months = int(useful_life_raw) if useful_life_raw else None
        degressive_rate = optional_decimal(request.form.get("degressive_rate", ""))
        total_units = optional_decimal(request.form.get("total_units", ""))
        residual_value = optional_decimal(request.form.get("residual_value", "")) or Decimal(
            "0.00"
        )
    except ValueError:
        flash("Datum, Betrag oder Nutzungsdauer ist ungültig.", "error")
        return redirect(url_for("main.fixed_assets_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            asset = create_fixed_asset(
                session=session,
                payload=FixedAssetInput(
                    company_id=company_id,
                    asset_number=asset_number,
                    name=name,
                    acquisition_date=acquisition_date,
                    in_service_date=in_service_date,
                    acquisition_cost=acquisition_cost,
                    method=method,
                    useful_life_months=useful_life_months,
                    degressive_rate=degressive_rate,
                    total_units=total_units,
                    residual_value=residual_value,
                    keep_memo_value=request.form.get("keep_memo_value") == "1",
                    asset_account_id=asset_account_id,
                    depreciation_account_id=depreciation_account_id,
                    notes=request.form.get("notes", "").strip() or None,
                    changed_by=changed_by(),
                ),
            )
        except FixedAssetError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.fixed_assets_page", company_id=company_id))
        except IntegrityError:
            session.rollback()
            flash("Ein Anlagegut mit dieser Inventarnummer existiert bereits.", "error")
            return redirect(url_for("main.fixed_assets_page", company_id=company_id))

    flash(f"Anlagegut {asset.asset_number} wurde angelegt.", "success")
    return redirect(url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset.id))


@main_bp.post("/anlagen/<int:asset_id>/abschreiben")
def depreciate_fixed_asset_action(asset_id: int):
    company_id = request.form.get("company_id", type=int)
    fiscal_year = request.form.get("fiscal_year", type=int)
    units = safe_optional_decimal(request.form.get("units", ""))

    if not fiscal_year:
        flash("Bitte ein Wirtschaftsjahr angeben.", "error")
        return redirect(url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None:
            abort(404)
        require_company_access(session, asset.company_id)
        company_id = asset.company_id
        try:
            entry = post_depreciation(
                session=session,
                fixed_asset_id=asset_id,
                fiscal_year=fiscal_year,
                units=units,
                changed_by=changed_by(),
            )
        except FixedAssetError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id)
            )

    flash(
        f"AfA {fiscal_year}: {entry.amount} gebucht. Neuer Buchwert: {entry.book_value_after}.",
        "success",
    )
    return redirect(url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id))


@main_bp.post("/anlagen/<int:asset_id>/ausserplan")
def impair_fixed_asset_action(asset_id: int):
    company_id = request.form.get("company_id", type=int)
    fiscal_year = request.form.get("fiscal_year", type=int)
    amount_raw = request.form.get("amount", "").strip()

    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None:
            abort(404)
        require_company_access(session, asset.company_id)
        company_id = asset.company_id
        try:
            amount = parse_decimal(amount_raw)
        except ValueError:
            flash("Betrag ist ungültig.", "error")
            return redirect(
                url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id)
            )
        try:
            record_impairment(
                session=session,
                fixed_asset_id=asset_id,
                fiscal_year=fiscal_year or date.today().year,
                amount=amount,
                changed_by=changed_by(),
            )
        except FixedAssetError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id)
            )

    flash("Außerplanmäßige Abschreibung wurde gebucht.", "success")
    return redirect(url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id))


@main_bp.post("/anlagen/<int:asset_id>/abgang")
def dispose_fixed_asset_action(asset_id: int):
    company_id = request.form.get("company_id", type=int)
    proceeds = safe_optional_decimal(request.form.get("proceeds", ""))

    session_factory = get_session_factory()
    with session_factory() as session:
        asset = session.get(FixedAsset, asset_id)
        if asset is None:
            abort(404)
        require_company_access(session, asset.company_id)
        company_id = asset.company_id
        try:
            disposal_date = date.fromisoformat(
                request.form.get("disposal_date", "").strip() or date.today().isoformat()
            )
        except ValueError:
            flash("Abgangsdatum ist ungültig.", "error")
            return redirect(
                url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id)
            )
        try:
            dispose_fixed_asset(
                session=session,
                fixed_asset_id=asset_id,
                disposal_date=disposal_date,
                proceeds=proceeds,
                changed_by=changed_by(),
            )
        except FixedAssetError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id)
            )

    flash("Anlagenabgang wurde gebucht.", "success")
    return redirect(url_for("main.fixed_assets_page", company_id=company_id, asset_id=asset_id))
