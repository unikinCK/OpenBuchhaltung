"""Zahlungsavise in der Oberfläche: hochladen und auslesen lassen, Zuordnung prüfen, ausgleichen."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from werkzeug.utils import secure_filename

from app.services.audit_log import log_audit_event
from app.services.documents import document_file_metadata
from app.services.llm_settings import PURPOSE_RECEIPT, receipt_analysis_options
from app.services.payment_advice import (
    CATEGORY_LABELS,
    LIGHT_LABELS,
    STATUS_APPLIED,
    STATUS_READY,
    STATUS_RUNNING,
    PaymentAdviceError,
    apply_advice,
    delete_advice,
    get_advice,
    list_advices,
    rematch_advice,
    start_advice,
)
from app.services.scoping import scoped_select
from app.services.standard_accounts import GEWAEHRTE_SKONTI, standard_account_id
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    document_upload_error,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, BankTransaction, Document, PaymentAdvice, TaxCode


def _form_int(name: str) -> int | None:
    value = request.form.get(name, type=int)
    return value or None


@main_bp.get("/opos/avise")
def payment_advices_page():
    with get_session_factory()() as session:
        companies, selected_company_id = company_context(session)
        advices = list_advices(session, selected_company_id) if selected_company_id else []
        documents: list[Document] = []
        if selected_company_id:
            used = select(PaymentAdvice.document_id).where(
                PaymentAdvice.company_id == selected_company_id
            )
            documents = list(
                session.execute(
                    scoped_select(Document, company_id=selected_company_id)
                    .where(
                        Document.journal_entry_id.is_(None),
                        Document.is_current.is_(True),
                        Document.id.not_in(used),
                    )
                    .order_by(Document.uploaded_at.desc())
                    .limit(50)
                ).scalars()
            )
        return render_template(
            "zahlungsavise.html",
            companies=companies,
            selected_company_id=selected_company_id,
            advices=advices,
            documents=documents,
            light_labels=LIGHT_LABELS,
            running=any(advice.status == STATUS_RUNNING for advice in advices),
            today=date.today().isoformat(),
        )


@main_bp.post("/opos/avise")
def start_payment_advice_action():
    company_id = request.form.get("company_id", type=int)
    document_id = request.form.get("document_id", type=int)
    uploaded_file = request.files.get("advice_file")
    if not company_id:
        abort(400)
    redirect_target = url_for("main.payment_advices_page", company_id=company_id)

    file_bytes = b""
    original_file_name = ""
    if not document_id:
        if uploaded_file is None or not uploaded_file.filename:
            flash("Bitte ein Avis hochladen oder einen Beleg wählen.", "error")
            return redirect(redirect_target)
        original_file_name = secure_filename(uploaded_file.filename)
        if not original_file_name:
            flash("Ungültiger Dateiname.", "error")
            return redirect(redirect_target)
        upload_error = document_upload_error(uploaded_file, original_file_name)
        if upload_error:
            flash(upload_error, "error")
            return redirect(redirect_target)
        file_bytes = uploaded_file.read()

    with get_session_factory()() as session:
        company = require_company_access(session, company_id)
        if not document_id:
            document_id = _store_upload(
                session,
                company=company,
                file_name=original_file_name,
                file_bytes=file_bytes,
                mime_type=uploaded_file.mimetype or "application/octet-stream",
            )
        try:
            advice = start_advice(
                session=session,
                company_id=company.id,
                document_id=document_id,
                changed_by=changed_by(),
                analysis_options=receipt_analysis_options(
                    llm_purpose=PURPOSE_RECEIPT, session=session
                ),
            )
        except PaymentAdviceError as exc:
            flash(str(exc), "error")
            return redirect(redirect_target)
        flash("Das Avis wird ausgelesen; mit lokalen Modellen dauert das Minuten.", "success")
        return redirect(
            url_for("main.payment_advice_detail_page", advice_id=advice.id, company_id=company.id)
        )


def _store_upload(session, *, company, file_name: str, file_bytes: bytes, mime_type: str) -> int:
    """Legt das hochgeladene Avis als Beleg ab (wie unter Belege)."""
    company_dir = (
        Path(current_app.config["DOCUMENT_UPLOAD_DIR"]) / str(company.tenant_id) / str(company.id)
    )
    company_dir.mkdir(parents=True, exist_ok=True)
    target_path = company_dir / f"{uuid4().hex}_{file_name}"
    target_path.write_bytes(file_bytes)
    metadata = document_file_metadata(file_bytes)
    document = Document(
        tenant_id=company.tenant_id,
        company_id=company.id,
        file_name=file_name,
        storage_key=str(target_path),
        mime_type=mime_type,
        file_sha256=metadata.file_sha256,
        file_size_bytes=metadata.file_size_bytes,
        document_date=date.today(),
        capture_source="web",
    )
    session.add(document)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="document",
        entity_id=str(document.id),
        action="uploaded",
        changed_by=changed_by(),
        payload={
            "file_name": document.file_name,
            "mime_type": document.mime_type,
            "file_sha256": document.file_sha256,
            "file_size_bytes": document.file_size_bytes,
            "document_date": document.document_date.isoformat(),
        },
    )
    session.commit()
    return document.id


def _load(session, advice_id: int):
    company_id = request.values.get("company_id", type=int)
    if not company_id:
        abort(400)
    company = require_company_access(session, company_id)
    try:
        return company, get_advice(session, company.id, advice_id)
    except PaymentAdviceError:
        abort(404)


@main_bp.get("/opos/avise/<int:advice_id>")
def payment_advice_detail_page(advice_id: int):
    with get_session_factory()() as session:
        companies, _selected = company_context(session)
        company, advice = _load(session, advice_id)
        accounts = list(
            session.execute(
                scoped_select(Account, company_id=company.id)
                .where(Account.is_active.is_(True))
                .order_by(Account.code)
            ).scalars()
        )
        booking_accounts = [
            a for a in accounts if a.account_type in {"income", "revenue", "expense"}
        ]
        tax_codes = list(
            session.execute(
                scoped_select(TaxCode, company_id=company.id)
                .where(TaxCode.is_active.is_(True))
                .order_by(TaxCode.code)
            ).scalars()
        )
        bank_transaction = (
            session.get(BankTransaction, advice.bank_transaction_id)
            if advice.bank_transaction_id
            else None
        )
        return render_template(
            "zahlungsavis.html",
            companies=companies,
            selected_company_id=company.id,
            advice=advice,
            result=advice.result or {},
            light_labels=LIGHT_LABELS,
            category_labels=CATEGORY_LABELS,
            booking_accounts=booking_accounts,
            default_discount_account_id=standard_account_id(booking_accounts, GEWAEHRTE_SKONTI),
            tax_codes=tax_codes,
            bank_transaction=bank_transaction,
            can_apply=advice.status == STATUS_READY
            or (
                advice.status == STATUS_APPLIED
                and (advice.journal_entry is None or advice.journal_entry.reversed_by is not None)
            ),
        )


@main_bp.post("/opos/avise/<int:advice_id>/zuordnen")
def rematch_payment_advice_action(advice_id: int):
    with get_session_factory()() as session:
        company, advice = _load(session, advice_id)
        try:
            rematch_advice(session=session, advice=advice, changed_by=changed_by())
            flash("Das Avis wurde neu zugeordnet.", "success")
        except PaymentAdviceError as exc:
            flash(str(exc), "error")
        return redirect(
            url_for("main.payment_advice_detail_page", advice_id=advice_id, company_id=company.id)
        )


@main_bp.post("/opos/avise/<int:advice_id>/ausgleichen")
def apply_payment_advice_action(advice_id: int):
    with get_session_factory()() as session:
        company, advice = _load(session, advice_id)
        bank_transaction_id = _form_int("bank_transaction_id")
        if not bank_transaction_id:
            flash("Bitte den Zahlungseingang wählen.", "error")
        else:
            try:
                advice = apply_advice(
                    session=session,
                    advice=advice,
                    bank_transaction_id=bank_transaction_id,
                    changed_by=changed_by(),
                    discount_account_id=_form_int("discount_account_id"),
                    discount_tax_code_id=_form_int("discount_tax_code_id"),
                    deduction_account_id=_form_int("deduction_account_id"),
                    deduction_tax_code_id=_form_int("deduction_tax_code_id"),
                )
                flash(
                    f"Ausgeglichen und gebucht ({advice.journal_entry.posting_number}).",
                    "success",
                )
            except PaymentAdviceError as exc:
                flash(str(exc), "error")
        return redirect(
            url_for("main.payment_advice_detail_page", advice_id=advice_id, company_id=company.id)
        )


@main_bp.post("/opos/avise/<int:advice_id>/loeschen")
def delete_payment_advice_action(advice_id: int):
    with get_session_factory()() as session:
        company, advice = _load(session, advice_id)
        try:
            delete_advice(session=session, advice=advice, changed_by=changed_by())
            flash("Das Avis wurde gelöscht, der Beleg bleibt erhalten.", "success")
        except PaymentAdviceError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for(
                    "main.payment_advice_detail_page", advice_id=advice_id, company_id=company.id
                )
            )
        return redirect(url_for("main.payment_advices_page", company_id=company.id))
