from __future__ import annotations

import csv
from datetime import date
from io import StringIO
from pathlib import Path
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app.auth import current_tenant_id, current_user, require_ui_login
from app.services.account_hierarchy import resolve_parent_account_id
from app.services.audit_log import log_audit_event
from app.services.document_llm import DocumentLLMError, send_document_update
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.reports import (
    balance_sheet_for_company,
    income_statement_for_company,
    trial_balance_for_company,
)
from app.services.scoping import scoped_select
from domain.models import (
    Account,
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
    Tenant,
)
from domain.services.journal_entry_validation import JournalEntryValidationError

main_bp = Blueprint("main", __name__)
main_bp.before_request(require_ui_login)


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


def _require_company_access(session, company_id: int) -> Company:
    """Load a company and enforce that it belongs to the user's tenant scope."""
    company = session.get(Company, company_id)
    if company is None:
        abort(404)
    tenant_id = current_tenant_id()
    if tenant_id is not None and company.tenant_id != tenant_id:
        abort(404)
    return company


def _changed_by() -> str:
    user = current_user()
    return user["username"] if user else "web-form"


def _company_context(session) -> tuple[list[Company], int | None]:
    """Companies im Tenant-Scope plus validierte Auswahl aus ?company_id=."""
    tenant_scope = current_tenant_id()
    companies = (
        session.execute(scoped_select(Company, tenant_id=tenant_scope).order_by(Company.name))
        .scalars()
        .all()
    )

    selected_company_id = request.args.get("company_id", type=int)
    accessible_ids = {company.id for company in companies}
    if selected_company_id is not None and selected_company_id not in accessible_ids:
        selected_company_id = None
    if selected_company_id is None and companies:
        selected_company_id = companies[0].id
    return companies, selected_company_id


@main_bp.get("/")
def index():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = _company_context(session)

        stats = {"accounts": 0, "journal_entries": 0, "documents": 0}
        totals = None
        balance_totals = None
        recent_entries = []
        if selected_company_id:
            stats["accounts"] = len(
                session.execute(scoped_select(Account, company_id=selected_company_id))
                .scalars()
                .all()
            )
            stats["documents"] = len(
                session.execute(scoped_select(Document, company_id=selected_company_id))
                .scalars()
                .all()
            )
            entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
            stats["journal_entries"] = len(entries)
            recent_entries = entries[:5]
            totals = income_statement_for_company(
                session=session, company_id=selected_company_id
            )["totals"]
            balance_totals = balance_sheet_for_company(
                session=session, company_id=selected_company_id
            )["totals"]

    return render_template(
        "dashboard.html",
        companies=companies,
        selected_company_id=selected_company_id,
        stats=stats,
        totals=totals,
        balance_totals=balance_totals,
        recent_entries=recent_entries,
    )


@main_bp.get("/buchungen")
def journal_page():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = _company_context(session)

        accounts = []
        tax_codes = []
        journal_entries = []
        lines_by_entry: dict[int, list[dict]] = {}
        if selected_company_id:
            accounts = (
                session.execute(
                    scoped_select(Account, company_id=selected_company_id).order_by(Account.code)
                )
                .scalars()
                .all()
            )
            tax_codes = (
                session.execute(
                    scoped_select(TaxCode, company_id=selected_company_id)
                    .where(TaxCode.is_active.is_(True))
                    .order_by(TaxCode.code)
                )
                .scalars()
                .all()
            )
            journal_entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
            line_rows = session.execute(
                select(
                    JournalEntryLine.journal_entry_id,
                    JournalEntryLine.line_number,
                    Account.code,
                    Account.name,
                    JournalEntryLine.debit_amount,
                    JournalEntryLine.credit_amount,
                    JournalEntryLine.description,
                )
                .join(Account, Account.id == JournalEntryLine.account_id)
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .where(JournalEntry.company_id == selected_company_id)
                .order_by(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number)
            ).all()
            for row in line_rows:
                lines_by_entry.setdefault(row.journal_entry_id, []).append(
                    {
                        "line_number": row.line_number,
                        "account_code": row.code,
                        "account_name": row.name,
                        "debit_amount": row.debit_amount,
                        "credit_amount": row.credit_amount,
                        "description": row.description,
                    }
                )

    return render_template(
        "buchungen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        tax_codes=tax_codes,
        journal_entries=journal_entries,
        lines_by_entry=lines_by_entry,
        today=date.today().isoformat(),
    )


@main_bp.get("/konten")
def accounts_page():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = _company_context(session)
        accounts = (
            session.execute(
                scoped_select(Account, company_id=selected_company_id).order_by(Account.code)
            )
            .scalars()
            .all()
            if selected_company_id
            else []
        )

    return render_template(
        "konten.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
    )


@main_bp.get("/belege")
def documents_page():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = _company_context(session)

        documents = []
        journal_entries = []
        if selected_company_id:
            documents = (
                session.execute(
                    scoped_select(Document, company_id=selected_company_id).order_by(
                        Document.uploaded_at.desc()
                    )
                )
                .scalars()
                .all()
            )
            journal_entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
        journal_entry_labels = {entry.id: entry.posting_number for entry in journal_entries}

    return render_template(
        "belege.html",
        companies=companies,
        selected_company_id=selected_company_id,
        documents=documents,
        journal_entries=journal_entries,
        journal_entry_labels=journal_entry_labels,
    )


@main_bp.get("/berichte")
def reports_page():
    session_factory = _get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = _company_context(session)

        trial_balance = []
        income_statement = {"revenues": [], "expenses": [], "totals": {}}
        balance_sheet = {"assets": [], "liabilities_and_equity": [], "totals": {}}
        if selected_company_id:
            trial_balance = trial_balance_for_company(
                session=session, company_id=selected_company_id
            )
            income_statement = income_statement_for_company(
                session=session, company_id=selected_company_id
            )
            balance_sheet = balance_sheet_for_company(
                session=session, company_id=selected_company_id
            )

    return render_template(
        "berichte.html",
        companies=companies,
        selected_company_id=selected_company_id,
        trial_balance=trial_balance,
        income_statement=income_statement,
        balance_sheet=balance_sheet,
    )


@main_bp.get("/verwaltung")
def admin_page():
    tenant_scope = current_tenant_id()
    session_factory = _get_session_factory()
    with session_factory() as session:
        tenant_query = scoped_select(Tenant).order_by(Tenant.name)
        if tenant_scope is not None:
            tenant_query = tenant_query.where(Tenant.id == tenant_scope)
        tenants = session.execute(tenant_query).scalars().all()
        companies, selected_company_id = _company_context(session)
        account_count = (
            len(
                session.execute(scoped_select(Account, company_id=selected_company_id))
                .scalars()
                .all()
            )
            if selected_company_id
            else 0
        )

    return render_template(
        "verwaltung.html",
        tenants=tenants,
        companies=companies,
        selected_company_id=selected_company_id,
        account_count=account_count,
        is_global_admin=tenant_scope is None,
    )


@main_bp.post("/tenants")
def create_tenant_and_company():
    if current_tenant_id() is not None:
        flash("Neue Mandanten kann nur ein globaler Administrator anlegen.", "error")
        return redirect(url_for("main.admin_page"))

    tenant_name = request.form.get("tenant_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

    if not tenant_name or not company_name:
        flash("Mandant und Gesellschaft sind Pflichtfelder.", "error")
        return redirect(url_for("main.admin_page"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenant = Tenant(name=tenant_name)
        company = Company(name=company_name, currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])

        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Mandant oder Gesellschaft existiert bereits.", "error")
            return redirect(url_for("main.admin_page"))

    flash("Mandant und Gesellschaft wurden angelegt.", "success")
    return redirect(url_for("main.admin_page"))


@main_bp.post("/accounts")
def create_account():
    company_id = request.form.get("company_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    account_type = request.form.get("account_type", "").strip()

    if not company_id or not code or not name or not account_type:
        flash("Alle Felder für das Konto müssen ausgefüllt sein.", "error")
        return redirect(url_for("main.accounts_page"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = _require_company_access(session, company_id)

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
            parent_account_id=resolve_parent_account_id(
                session=session, company_id=company.id, code=code
            ),
        )
        session.add(account)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.accounts_page", company_id=company_id))


@main_bp.post("/journal-entries")
def create_journal_entry_from_form():
    company_id = request.form.get("company_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()
    if not company_id or not entry_date_raw or not description:
        flash("Gesellschaft, Datum und Beschreibung sind Pflichtfelder.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    try:
        parsed_date = date.fromisoformat(entry_date_raw)
    except ValueError:
        flash("Ungültiges Datum.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    try:
        line_inputs: list[JournalLineInput] = []
        line_account_ids = request.form.getlist("line_account_id")
        line_sides = request.form.getlist("line_side")
        line_amounts = request.form.getlist("line_amount")
        line_descriptions = request.form.getlist("line_description")
        line_tax_code_ids = request.form.getlist("line_tax_code_id")

        # Backward-compatible fallback (legacy single amount + Soll/Haben Felder)
        if not line_account_ids:
            debit_account_id = request.form.get("debit_account_id", type=int)
            credit_account_id = request.form.get("credit_account_id", type=int)
            amount_raw = request.form.get("amount", "").strip()
            if not debit_account_id or not credit_account_id:
                flash("Bitte Soll- und Habenkonto auswählen.", "error")
                return redirect(url_for("main.journal_page", company_id=company_id))
            amount = parse_decimal(amount_raw)
            line_inputs = [
                JournalLineInput(
                    account_id=debit_account_id,
                    debit_amount=amount,
                    credit_amount=parse_decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=credit_account_id,
                    debit_amount=parse_decimal("0.00"),
                    credit_amount=amount,
                ),
            ]
        else:
            max_len = max(len(line_account_ids), len(line_sides), len(line_amounts))
            for idx in range(max_len):
                account_raw = (line_account_ids[idx] if idx < len(line_account_ids) else "").strip()
                side_raw = (line_sides[idx] if idx < len(line_sides) else "").strip()
                amount_raw = (line_amounts[idx] if idx < len(line_amounts) else "").strip()
                description_raw = (
                    line_descriptions[idx] if idx < len(line_descriptions) else ""
                ).strip()
                tax_code_raw = (
                    line_tax_code_ids[idx] if idx < len(line_tax_code_ids) else ""
                ).strip()
                if not account_raw and not amount_raw and not side_raw:
                    continue
                if not account_raw or not amount_raw or side_raw not in {"debit", "credit"}:
                    raise JournalEntryCreationError(
                        f"Zeile {idx + 1}: Konto, Seite (Soll/Haben) und Betrag sind erforderlich."
                    )
                amount = parse_decimal(amount_raw)
                if amount <= parse_decimal("0.00"):
                    raise JournalEntryCreationError(f"Zeile {idx + 1}: Betrag muss größer 0 sein.")
                line_inputs.append(
                    JournalLineInput(
                        account_id=int(account_raw),
                        debit_amount=amount if side_raw == "debit" else parse_decimal("0.00"),
                        credit_amount=amount if side_raw == "credit" else parse_decimal("0.00"),
                        description=description_raw or None,
                        tax_code_id=int(tax_code_raw) if tax_code_raw else None,
                    )
                )

            if len(line_inputs) < 2:
                raise JournalEntryCreationError("Bitte mindestens zwei Buchungszeilen erfassen.")

        entry_payload = JournalEntryInput(
            company_id=company_id,
            entry_date=parsed_date,
            description=description,
            status="posted",
            changed_by=_changed_by(),
            lines=line_inputs,
        )

        session_factory = _get_session_factory()
        with session_factory() as session:
            _require_company_access(session, company_id)
            entry = create_journal_entry(session=session, payload=entry_payload)

    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    flash(f"Buchung {entry.posting_number} wurde gespeichert.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/documents")
def upload_document():
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)
    uploaded_file = request.files.get("document_file")

    if not company_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft und Datei sind Pflichtfelder.", "error")
        return redirect(url_for("main.documents_page", company_id=company_id))

    original_file_name = secure_filename(uploaded_file.filename)
    if not original_file_name:
        flash("Ungültiger Dateiname.", "error")
        return redirect(url_for("main.documents_page", company_id=company_id))

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = _require_company_access(session, company_id)

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != company.id:
                flash("Ausgewählte Buchung wurde nicht gefunden.", "error")
                return redirect(url_for("main.documents_page", company_id=company_id))

        unique_name = f"{uuid4().hex}_{original_file_name}"
        tenant_dir = Path(current_app.config["DOCUMENT_UPLOAD_DIR"]) / str(company.tenant_id)
        company_dir = tenant_dir / str(company.id)
        company_dir.mkdir(parents=True, exist_ok=True)
        target_path = company_dir / unique_name
        uploaded_file.save(target_path)

        document = Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            journal_entry_id=linked_entry.id if linked_entry else None,
            file_name=original_file_name,
            storage_key=str(target_path),
            mime_type=uploaded_file.mimetype or "application/octet-stream",
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
            changed_by=_changed_by(),
            payload={
                "file_name": document.file_name,
                "journal_entry_id": document.journal_entry_id,
                "mime_type": document.mime_type,
            },
        )
        session.commit()

        uploaded_document_id = document.id
        uploaded_tenant_id = document.tenant_id
        uploaded_company_id = document.company_id
        uploaded_file_name = document.file_name
        uploaded_mime_type = document.mime_type
        uploaded_journal_entry_id = document.journal_entry_id

    llm_endpoint = current_app.config.get("DOCUMENT_LLM_ENDPOINT_URL")
    if llm_endpoint:
        try:
            llm_response = send_document_update(
                endpoint_url=llm_endpoint,
                model=current_app.config.get("DOCUMENT_LLM_MODEL", "gpt-4.1-mini"),
                company_id=company_id,
                document_id=uploaded_document_id,
                file_name=uploaded_file_name,
                mime_type=uploaded_mime_type,
                journal_entry_id=uploaded_journal_entry_id,
            )
            with session_factory() as session:
                log_audit_event(
                    session=session,
                    tenant_id=uploaded_tenant_id,
                    company_id=uploaded_company_id,
                    entity_type="document",
                    entity_id=str(uploaded_document_id),
                    action="llm_update_requested",
                    changed_by=_changed_by(),
                    payload={"status": "success", "response": llm_response},
                )
                session.commit()
        except DocumentLLMError as exc:
            current_app.logger.warning(
                "LLM request for document %s failed: %s",
                uploaded_document_id,
                exc.message,
            )
            with session_factory() as session:
                log_audit_event(
                    session=session,
                    tenant_id=uploaded_tenant_id,
                    company_id=uploaded_company_id,
                    entity_type="document",
                    entity_id=str(uploaded_document_id),
                    action="llm_update_requested",
                    changed_by=_changed_by(),
                    payload={"status": "error", "message": exc.message},
                )
                session.commit()

    flash("Beleg wurde hochgeladen.", "success")
    return redirect(url_for("main.documents_page", company_id=company_id))


@main_bp.get("/documents/<int:document_id>/download")
def download_document(document_id: int):
    session_factory = _get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None:
            abort(404)
        tenant_scope = current_tenant_id()
        if tenant_scope is not None and document.tenant_id != tenant_scope:
            abort(404)
        document_path = Path(document.storage_key)
        if not document_path.exists():
            abort(404)

        return send_file(
            document_path,
            mimetype=document.mime_type,
            as_attachment=True,
            download_name=document.file_name,
        )


@main_bp.get("/reports/trial-balance.csv")
def download_trial_balance_csv():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        flash("Gesellschaft für Export fehlt.", "error")
        return redirect(url_for("main.reports_page"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        _require_company_access(session, company_id)
        rows = trial_balance_for_company(session=session, company_id=company_id)

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Konto", "Name", "Soll", "Haben", "Saldo"])
    for row in rows:
        writer.writerow(
            [
                row["code"],
                row["name"],
                f"{row['debit_total']:.2f}",
                f"{row['credit_total']:.2f}",
                f"{row['balance']:.2f}",
            ]
        )

    csv_content = output.getvalue()
    response = make_response(csv_content)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f"attachment; filename=susa-{company_id}-{date.today().isoformat()}.csv"
    )
    return response
