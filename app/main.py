from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    parse_decimal,
)
from app.services.reports import trial_balance_for_company
from app.services.scoping import scoped_select
from domain.models import Account, Company, Document, JournalEntry, Tenant
from domain.services.journal_entry_validation import JournalEntryValidationError

main_bp = Blueprint("main", __name__)


def _get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


@main_bp.get("/")
def index():
    selected_company_id = request.args.get("company_id", type=int)

    session_factory = _get_session_factory()
    with session_factory() as session:
        tenants = session.execute(scoped_select(Tenant).order_by(Tenant.name)).scalars().all()
        companies = session.execute(scoped_select(Company).order_by(Company.name)).scalars().all()

        if selected_company_id is None and companies:
            selected_company_id = companies[0].id

        account_query = scoped_select(
            Account,
            company_id=selected_company_id,
        ).order_by(Account.code)
        accounts = session.execute(account_query).scalars().all() if selected_company_id else []

        trial_balance = (
            trial_balance_for_company(session=session, company_id=selected_company_id)
            if selected_company_id
            else []
        )
        journal_entries = (
            session.execute(
                scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                    JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                )
            )
            .scalars()
            .all()
            if selected_company_id
            else []
        )
        documents = (
            session.execute(
                scoped_select(Document, company_id=selected_company_id).order_by(
                    Document.uploaded_at.desc()
                )
            )
            .scalars()
            .all()
            if selected_company_id
            else []
        )
        journal_entry_labels = {entry.id: entry.posting_number for entry in journal_entries}

    return render_template(
        "index.html",
        tenants=tenants,
        companies=companies,
        accounts=accounts,
        selected_company_id=selected_company_id,
        trial_balance=trial_balance,
        journal_entries=journal_entries,
        documents=documents,
        journal_entry_labels=journal_entry_labels,
        today=date.today().isoformat(),
    )


@main_bp.post("/tenants")
def create_tenant_and_company():
    tenant_name = request.form.get("tenant_name", "").strip()
    company_name = request.form.get("company_name", "").strip()

    if not tenant_name or not company_name:
        flash("Mandant und Gesellschaft sind Pflichtfelder.", "error")
        return redirect(url_for("main.index"))

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
            return redirect(url_for("main.index"))

    flash("Mandant und Gesellschaft wurden angelegt.", "success")
    return redirect(url_for("main.index"))


@main_bp.post("/accounts")
def create_account():
    company_id = request.form.get("company_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    account_type = request.form.get("account_type", "").strip()

    if not company_id or not code or not name or not account_type:
        flash("Alle Felder für das Konto müssen ausgefüllt sein.", "error")
        return redirect(url_for("main.index"))

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            abort(404)

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
        )
        session.add(account)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.index", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.index", company_id=company_id))


@main_bp.post("/journal-entries")
def create_journal_entry_from_form():
    company_id = request.form.get("company_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()
    if not company_id or not entry_date_raw or not description:
        flash("Gesellschaft, Datum und Beschreibung sind Pflichtfelder.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    try:
        parsed_date = date.fromisoformat(entry_date_raw)
    except ValueError:
        flash("Ungültiges Datum.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    try:
        line_inputs: list[JournalLineInput] = []
        line_account_ids = request.form.getlist("line_account_id")
        line_sides = request.form.getlist("line_side")
        line_amounts = request.form.getlist("line_amount")
        line_descriptions = request.form.getlist("line_description")

        # Backward-compatible fallback (legacy single amount + Soll/Haben Felder)
        if not line_account_ids:
            debit_account_id = request.form.get("debit_account_id", type=int)
            credit_account_id = request.form.get("credit_account_id", type=int)
            amount_raw = request.form.get("amount", "").strip()
            if not debit_account_id or not credit_account_id:
                flash("Bitte Soll- und Habenkonto auswählen.", "error")
                return redirect(url_for("main.index", company_id=company_id))
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
                    )
                )

            if len(line_inputs) < 2:
                raise JournalEntryCreationError("Bitte mindestens zwei Buchungszeilen erfassen.")

        entry_payload = JournalEntryInput(
            company_id=company_id,
            entry_date=parsed_date,
            description=description,
            status="posted",
            changed_by="web-form",
            lines=line_inputs,
        )

        session_factory = _get_session_factory()
        with session_factory() as session:
            entry = create_journal_entry(session=session, payload=entry_payload)

    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index", company_id=company_id))

    flash(f"Buchung {entry.posting_number} wurde gespeichert.", "success")
    return redirect(url_for("main.index", company_id=company_id))


@main_bp.post("/documents")
def upload_document():
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)
    uploaded_file = request.files.get("document_file")

    if not company_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft und Datei sind Pflichtfelder.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    original_file_name = secure_filename(uploaded_file.filename)
    if not original_file_name:
        flash("Ungültiger Dateiname.", "error")
        return redirect(url_for("main.index", company_id=company_id))

    session_factory = _get_session_factory()
    with session_factory() as session:
        company = session.get(Company, company_id)
        if company is None:
            abort(404)

        linked_entry = None
        if journal_entry_id is not None:
            linked_entry = session.get(JournalEntry, journal_entry_id)
            if linked_entry is None or linked_entry.company_id != company.id:
                flash("Ausgewählte Buchung wurde nicht gefunden.", "error")
                return redirect(url_for("main.index", company_id=company_id))

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
        session.commit()

    flash("Beleg wurde hochgeladen.", "success")
    return redirect(url_for("main.index", company_id=company_id))


@main_bp.get("/documents/<int:document_id>/download")
def download_document(document_id: int):
    session_factory = _get_session_factory()
    with session_factory() as session:
        document = session.get(Document, document_id)
        if document is None:
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
