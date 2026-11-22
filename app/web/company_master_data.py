"""Verwaltung: Firmendaten (Anschrift, Steuer, Register, Rechnungspräfix)."""

from __future__ import annotations

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from app.services.company_master_data import (
    TEXT_FIELD_LIMITS,
    CompanyMasterDataError,
    resolve_seller,
    update_company_master_data,
)
from app.services.elster_certificates import (
    MAX_CERTIFICATE_BYTES,
    ElsterCertificateError,
    clear_company_elster_certificate,
    get_company_elster_certificate,
    serialize_company_elster_certificate,
    set_company_elster_certificate,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Company

_FORM_FIELDS = (
    *TEXT_FIELD_LIMITS,
    "country_code",
    "vat_id",
    "tax_office_number",
    "email",
    "invoice_prefix",
)


@main_bp.get("/verwaltung/firmendaten")
def company_master_data_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        company = session.get(Company, selected_company_id) if selected_company_id else None
        return render_template(
            "firmendaten.html",
            companies=companies,
            selected_company_id=selected_company_id,
            company=company,
            seller=resolve_seller(company) if company else None,
            elster_certificate=serialize_company_elster_certificate(
                get_company_elster_certificate(session, company.id), company_id=company.id
            )
            if company
            else None,
        )


@main_bp.post("/verwaltung/firmendaten")
def update_company_master_data_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    fields = {field: request.form[field] for field in _FORM_FIELDS if field in request.form}
    fields["is_small_business"] = request.form.get("is_small_business") == "1"
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            changed = update_company_master_data(
                session=session, company=company, changed_by=changed_by(), **fields
            )
        except CompanyMasterDataError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.company_master_data_page", company_id=company_id))
        session.commit()
    flash("Firmendaten gespeichert." if changed else "Keine Änderung.", "success")
    return redirect(url_for("main.company_master_data_page", company_id=company_id))


def _master_data_redirect(company_id: int):
    return redirect(
        url_for("main.company_master_data_page", company_id=company_id) + "#elster-zertifikat"
    )


@main_bp.post("/verwaltung/firmendaten/elster-zertifikat")
def set_company_elster_certificate_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    upload = request.files.get("file")
    content = upload.read(MAX_CERTIFICATE_BYTES + 1) if upload is not None else b""
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            set_company_elster_certificate(
                session=session,
                company=company,
                content=content,
                pin=request.form.get("pin") or "",
                alias=request.form.get("alias"),
                file_name=upload.filename if upload is not None else None,
                changed_by=changed_by(),
                secret_key=current_app.config.get("SECRET_KEY"),
            )
        except ElsterCertificateError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _master_data_redirect(company_id)
        session.commit()
    flash("ELSTER-Zertifikat gespeichert.", "success")
    return _master_data_redirect(company_id)


@main_bp.post("/verwaltung/firmendaten/elster-zertifikat/loeschen")
def clear_company_elster_certificate_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        deleted = clear_company_elster_certificate(
            session=session, company=company, changed_by=changed_by()
        )
        session.commit()
    flash("ELSTER-Zertifikat gelöscht." if deleted else "Kein Zertifikat hinterlegt.", "success")
    return _master_data_redirect(company_id)
