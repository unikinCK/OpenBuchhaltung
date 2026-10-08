"""Geschäftspartner (Kunden/Debitoren, Lieferanten/Kreditoren): Liste, Anlage, Pflege."""

from __future__ import annotations

from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from app.services.partners import (
    PartnerError,
    create_partner,
    duplicate_hints,
    list_partners,
    partner_history,
    set_partner_bank_details,
    update_partner,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    pagination_args,
    require_company_access,
)
from domain.models import SUBLEDGER_TYPES, BusinessPartner

_TEXT_FIELDS = (
    "name",
    "partner_kind",
    "street",
    "postal_code",
    "city",
    "country_code",
    "vat_id",
    "tax_number",
    "email",
    "phone",
    "contact_person",
    "payment_term_days",
    "notes",
    "debtor_number",
    "creditor_number",
)


def _form_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {
        field: request.form.get(field, "") for field in _TEXT_FIELDS if field in request.form
    }
    fields["is_customer"] = request.form.get("is_customer") == "1"
    fields["is_supplier"] = request.form.get("is_supplier") == "1"
    # Ohne Rolle zählt eine (vorbelegte) Nummer nicht – die Rolle wird entfernt.
    if not fields["is_customer"]:
        fields.pop("debtor_number", None)
    if not fields["is_supplier"]:
        fields.pop("creditor_number", None)
    return fields


def _flash_hints(hints: list[str]) -> None:
    for hint in hints:
        flash(f"Hinweis: {hint}", "warning")


@main_bp.get("/partner")
def partners_page():
    limit, offset = pagination_args()
    role = (request.args.get("role") or "").strip() or None
    if role not in SUBLEDGER_TYPES:
        role = None
    query = (request.args.get("q") or "").strip() or None
    include_inactive = request.args.get("include_inactive") == "1"
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        partners: list[BusinessPartner] = []
        total = 0
        if selected_company_id:
            partners, total = list_partners(
                session=session,
                company_id=selected_company_id,
                role=role,
                query=query,
                include_inactive=include_inactive,
                limit=limit,
                offset=offset,
            )
    filter_args = {
        key: value
        for key, value in (
            ("role", role),
            ("q", query),
            ("include_inactive", "1" if include_inactive else None),
        )
        if value
    }
    return render_template(
        "partner.html",
        companies=companies,
        selected_company_id=selected_company_id,
        partners=partners,
        total=total,
        limit=limit,
        offset=offset,
        role=role,
        q=query,
        include_inactive=include_inactive,
        filter_args=filter_args,
    )


@main_bp.post("/partner")
def create_partner_from_form():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            partner = create_partner(
                session=session,
                company=company,
                changed_by=changed_by(),
                **_form_fields(),
            )
            iban = request.form.get("iban", "").strip()
            bic = request.form.get("bic", "").strip()
            if iban or bic:
                set_partner_bank_details(
                    session=session,
                    partner=partner,
                    changed_by=changed_by(),
                    iban=iban,
                    bic=bic,
                )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partners_page", company_id=company_id))
        hints = duplicate_hints(
            session=session,
            company_id=company.id,
            name=partner.name,
            vat_id=partner.vat_id,
            iban=partner.iban,
            exclude_id=partner.id,
        )
        session.commit()
        partner_id = partner.id
        partner_name = partner.name
    flash(f"Geschäftspartner „{partner_name}“ wurde angelegt.", "success")
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))


def _load_partner(session, partner_id: int) -> BusinessPartner:
    partner = session.get(BusinessPartner, partner_id)
    if partner is None:
        abort(404)
    require_company_access(session, partner.company_id)
    return partner


@main_bp.get("/partner/<int:partner_id>")
def partner_detail_page(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        companies, _ = company_context(session)
        history = partner_history(session=session, partner_id=partner.id, limit=100)
        return render_template(
            "partner_detail.html",
            companies=companies,
            selected_company_id=partner.company_id,
            partner=partner,
            history=history,
        )


@main_bp.post("/partner/<int:partner_id>/update")
def update_partner_from_form(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        fields = _form_fields()
        fields["is_active"] = request.form.get("is_active") == "true"
        try:
            changed = update_partner(
                session=session,
                partner=partner,
                changed_by=changed_by(),
                **fields,
            )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
        hints = duplicate_hints(
            session=session,
            company_id=partner.company_id,
            name=partner.name,
            vat_id=partner.vat_id,
            exclude_id=partner.id,
        )
        session.commit()
    flash("Geschäftspartner wurde geändert." if changed else "Keine Änderung erkannt.", "success")
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))


@main_bp.post("/partner/<int:partner_id>/bankdaten")
def set_partner_bank_details_from_form(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        try:
            changed = set_partner_bank_details(
                session=session,
                partner=partner,
                changed_by=changed_by(),
                iban=request.form.get("iban", ""),
                bic=request.form.get("bic", ""),
            )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
        hints = (
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
    flash(
        "Bankverbindung wurde gespeichert." if changed else "Keine Änderung erkannt.",
        "success",
    )
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
