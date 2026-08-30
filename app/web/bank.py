"""Bankumsätze: Kontoauszugs-Import (CSV/CAMT/MT940), Matching und Verbuchung."""

from __future__ import annotations

from datetime import date

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask import session as flask_session
from sqlalchemy import func, select

from app.services.bank_import import (
    BankImportError,
    bank_reconciliation,
    book_transaction,
    detect_transfer_counterparts,
    find_geldtransit_account,
    import_bank_statement,
    match_transaction,
    move_bank_transactions,
    reassign_bank_transactions,
    suggest_matches_for,
)
from app.services.fints_sync import (
    FinTSSyncError,
    FinTSSyncResult,
    cancel_pending_dialog,
    create_fints_connection,
    list_fints_connections,
    set_fints_connection_active,
    start_fints_sync,
    submit_fints_tan,
)
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    filter_url_args,
    get_session_factory,
    pagination_args,
    require_company_access,
    search_args,
)
from domain.models import (
    Account,
    BankTransaction,
    ControllingUnit,
    FinTSConnection,
    FinTSPendingDialog,
    JournalEntry,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/bank")
def bank_page():
    limit, offset = pagination_args()
    status_filter = (request.args.get("status") or "").strip() or None
    query, date_from, date_to = search_args()
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        bank_accounts = []
        contra_accounts = []
        tax_codes = []
        transactions = []
        transactions_total = 0
        cost_centers = []
        profit_centers = []
        fints_connections = []
        suggestions_by_tx: dict[int, list[JournalEntry]] = {}
        transfer_by_tx: dict[int, BankTransaction] = {}
        geldtransit_account = None
        reconciliation = []
        if selected_company_id:
            fints_connections = list_fints_connections(
                session=session, company_id=selected_company_id
            )
            accounts = (
                session.execute(
                    scoped_select(Account, company_id=selected_company_id).order_by(Account.code)
                )
                .scalars()
                .all()
            )
            bank_accounts = [account for account in accounts if account.account_type == "asset"]
            contra_accounts = accounts
            tax_codes = (
                session.execute(
                    scoped_select(TaxCode, company_id=selected_company_id)
                    .where(TaxCode.is_active.is_(True))
                    .order_by(TaxCode.code)
                )
                .scalars()
                .all()
            )
            transactions_stmt = scoped_select(BankTransaction, company_id=selected_company_id)
            if status_filter:
                transactions_stmt = transactions_stmt.where(
                    BankTransaction.status == status_filter
                )
            if query:
                pattern = f"%{query}%"
                transactions_stmt = transactions_stmt.where(
                    BankTransaction.purpose.ilike(pattern)
                    | BankTransaction.counterparty.ilike(pattern)
                    | BankTransaction.bank_reference.ilike(pattern)
                )
            if date_from:
                transactions_stmt = transactions_stmt.where(
                    BankTransaction.booking_date >= date_from
                )
            if date_to:
                transactions_stmt = transactions_stmt.where(
                    BankTransaction.booking_date <= date_to
                )
            transactions_total = session.execute(
                select(func.count()).select_from(transactions_stmt.subquery())
            ).scalar_one()
            transactions = (
                session.execute(
                    transactions_stmt.order_by(
                        BankTransaction.booking_date.desc(), BankTransaction.id.desc()
                    )
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            controlling_units = (
                session.execute(
                    scoped_select(ControllingUnit, company_id=selected_company_id)
                    .where(ControllingUnit.is_active.is_(True))
                    .order_by(ControllingUnit.code)
                )
                .scalars()
                .all()
            )
            cost_centers = [u for u in controlling_units if u.unit_type == "cost_center"]
            profit_centers = [u for u in controlling_units if u.unit_type == "profit_center"]
            suggestions_by_tx = suggest_matches_for(session=session, transactions=transactions)
            transfer_by_tx = detect_transfer_counterparts(
                session=session, transactions=transactions
            )
            geldtransit_account = find_geldtransit_account(
                session=session, company_id=selected_company_id
            )
            reconciliation = bank_reconciliation(
                session=session, company_id=selected_company_id
            )

    fints_challenge = flask_session.get("fints_challenge")
    if fints_challenge and fints_challenge.get("company_id") != selected_company_id:
        fints_challenge = None

    return render_template(
        "bank.html",
        companies=companies,
        selected_company_id=selected_company_id,
        bank_accounts=bank_accounts,
        bank_accounts_by_id={account.id: account for account in bank_accounts},
        contra_accounts=contra_accounts,
        tax_codes=tax_codes,
        transactions=transactions,
        transactions_total=transactions_total,
        limit=limit,
        offset=offset,
        status_filter=status_filter,
        q=query,
        date_from=date_from,
        date_to=date_to,
        filter_args=filter_url_args(query, date_from, date_to, status=status_filter),
        suggestions_by_tx=suggestions_by_tx,
        transfer_by_tx=transfer_by_tx,
        geldtransit_account=geldtransit_account,
        reconciliation=reconciliation,
        cost_centers=cost_centers,
        profit_centers=profit_centers,
        fints_connections=fints_connections,
        fints_challenge=fints_challenge,
        fints_configured=bool(current_app.config.get("FINTS_PRODUCT_ID")),
    )


@main_bp.post("/bank/import")
def bank_import_action():
    company_id = request.form.get("company_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    uploaded_file = request.files.get("bank_file") or request.files.get("bank_csv")

    if not company_id or not bank_account_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft, Bankkonto und Kontoauszugsdatei sind Pflichtfelder.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            report = import_bank_statement(
                session=session,
                company_id=company_id,
                bank_account_id=bank_account_id,
                file_name=uploaded_file.filename,
                content=uploaded_file.read(),
                changed_by=changed_by(),
            )
        except BankImportError as exc:
            flash(f"Import fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    flash(
        f"Bank-Import: {report.imported_rows} neu, {report.duplicate_rows} Duplikate, "
        f"{report.error_rows} Fehler.",
        "success" if report.error_rows == 0 else "error",
    )
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/<int:transaction_id>/zuordnen")
def bank_match_action(transaction_id: int):
    company_id = request.form.get("company_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)
    if not journal_entry_id:
        flash("Bitte eine Buchung für die Zuordnung auswählen.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None:
            abort(404)
        require_company_access(session, transaction.company_id)
        try:
            transaction = match_transaction(
                session=session,
                transaction_id=transaction_id,
                journal_entry_id=journal_entry_id,
                changed_by=changed_by(),
            )
        except BankImportError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    flash("Bankumsatz wurde der Buchung zugeordnet.", "success")
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/<int:transaction_id>/bankkonto")
def bank_reassign_action(transaction_id: int):
    company_id = request.form.get("company_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    if not bank_account_id:
        flash("Bitte ein Bankkonto auswählen.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None:
            abort(404)
        require_company_access(session, transaction.company_id)
        try:
            result = reassign_bank_transactions(
                session=session,
                transaction_ids=[transaction_id],
                bank_account_id=bank_account_id,
                changed_by=changed_by(),
                reclassify=request.form.get("reclassify", "1") == "1",
            )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))
        entry = result.reclassification_entry
        reclass_note = (
            f" Umgliederungsbuchung {entry.posting_number} erstellt." if entry else ""
        )

    if result:
        flash(f"Bankumsatz wurde auf das gewählte Bankkonto umgehängt.{reclass_note}", "success")
    else:
        flash("Bankumsatz liegt bereits auf diesem Bankkonto.", "warning")
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/umhaengen")
def bank_move_action():
    company_id = request.form.get("company_id", type=int)
    source_bank_account_id = request.form.get("source_bank_account_id", type=int)
    target_bank_account_id = request.form.get("target_bank_account_id", type=int)
    if not company_id or not source_bank_account_id or not target_bank_account_id:
        flash("Gesellschaft, Quell- und Zielkonto sind Pflichtfelder.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        reclassify_requested = request.form.get("reclassify", "1") == "1"
        try:
            result = move_bank_transactions(
                session=session,
                company_id=company_id,
                source_bank_account_id=source_bank_account_id,
                target_bank_account_id=target_bank_account_id,
                changed_by=changed_by(),
                reclassify=reclassify_requested,
            )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))
        entry = result.reclassification_entry

    if entry is not None:
        reclass_note = f" Saldo per Umgliederungsbuchung {entry.posting_number} mitgezogen."
    elif result and reclassify_requested:
        reclass_note = " Keine Umgliederung nötig (keine verbuchten Umsätze bewegt)."
    elif result:
        reclass_note = (
            " Bereits erzeugte Buchungen bleiben auf dem alten Konto — "
            "Saldo bei Bedarf umgliedern."
        )
    else:
        reclass_note = ""
    flash(
        f"{len(result.transactions)} Bankumsätze umgehängt.{reclass_note}",
        "success" if result else "warning",
    )
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/<int:transaction_id>/buchen")
def bank_book_action(transaction_id: int):
    company_id = request.form.get("company_id", type=int)
    contra_account_id = request.form.get("contra_account_id", type=int)
    tax_code_id = request.form.get("tax_code_id", type=int)
    if not contra_account_id:
        flash("Bitte ein Gegenkonto auswählen.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        transaction = session.get(BankTransaction, transaction_id)
        if transaction is None:
            abort(404)
        require_company_access(session, transaction.company_id)
        try:
            transaction = book_transaction(
                session=session,
                transaction_id=transaction_id,
                contra_account_id=contra_account_id,
                tax_code_id=tax_code_id,
                cost_center_id=request.form.get("cost_center_id", type=int),
                profit_center_id=request.form.get("profit_center_id", type=int),
                changed_by=changed_by(),
            )
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    flash("Bankumsatz wurde verbucht.", "success")
    return redirect(url_for("main.bank_page", company_id=company_id))


def _handle_fints_result(result: FinTSSyncResult, company_id: int | None):
    if result.challenge is not None:
        # company_id bindet die TAN-Karte an die richtige Gesellschaft —
        # bei einem Wechsel der Gesellschaft wird sie nicht angezeigt.
        flask_session["fints_challenge"] = {
            "dialog_id": result.challenge.dialog_id,
            "challenge": result.challenge.challenge,
            "decoupled": result.challenge.decoupled,
            "company_id": company_id,
        }
        flash("Die Bank verlangt eine TAN-Bestätigung.", "info")
        return redirect(url_for("main.bank_page", company_id=company_id))

    flask_session.pop("fints_challenge", None)
    report = result.report
    flash(
        f"FinTS-Abruf: {report.imported_rows} neu, {report.duplicate_rows} Duplikate, "
        f"{report.error_rows} Fehler.",
        "success" if report.error_rows == 0 else "error",
    )
    return redirect(url_for("main.bank_page", company_id=company_id))


def _parse_form_date(field: str) -> date | None:
    raw = (request.form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise FinTSSyncError(f"Ungültiges Datum im Feld {field}.") from exc


@main_bp.post("/bank/fints/anlegen")
def fints_connection_create_action():
    company_id = request.form.get("company_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    if not company_id or not bank_account_id:
        flash("Gesellschaft und Bankkonto sind Pflichtfelder.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            connection = create_fints_connection(
                session=session,
                company_id=company_id,
                bank_account_id=bank_account_id,
                name=request.form.get("name") or "",
                blz=request.form.get("blz") or "",
                login=request.form.get("login") or "",
                fints_url=request.form.get("fints_url") or "",
                sepa_iban=request.form.get("sepa_iban"),
                changed_by=changed_by(),
            )
        except FinTSSyncError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    flash(f"Bankzugang „{connection.name}“ wurde angelegt.", "success")
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/fints/<int:connection_id>/deaktivieren")
def fints_connection_deactivate_action(connection_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None:
            abort(404)
        require_company_access(session, connection.company_id)
        company_id = connection.company_id
        try:
            set_fints_connection_active(
                session=session,
                connection_id=connection_id,
                is_active=False,
                changed_by=changed_by(),
            )
        except FinTSSyncError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    flash("Bankzugang wurde deaktiviert.", "success")
    return redirect(url_for("main.bank_page", company_id=company_id))


@main_bp.post("/bank/fints/<int:connection_id>/abrufen")
def fints_sync_action(connection_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        connection = session.get(FinTSConnection, connection_id)
        if connection is None:
            abort(404)
        require_company_access(session, connection.company_id)
        company_id = connection.company_id
        try:
            result = start_fints_sync(
                session=session,
                connection_id=connection_id,
                pin=request.form.get("pin") or "",
                product_id=current_app.config.get("FINTS_PRODUCT_ID"),
                from_date=_parse_form_date("from_date"),
                to_date=_parse_form_date("to_date"),
                changed_by=changed_by(),
            )
        except FinTSSyncError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    return _handle_fints_result(result, company_id)


@main_bp.post("/bank/fints/tan")
def fints_tan_action():
    company_id = request.form.get("company_id", type=int)
    dialog_id = (request.form.get("dialog_id") or "").strip()
    if not dialog_id:
        flash("TAN-Dialog fehlt.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        pending = session.get(FinTSPendingDialog, dialog_id)
        if pending is None:
            flask_session.pop("fints_challenge", None)
            flash("TAN-Dialog nicht gefunden oder bereits abgeschlossen.", "error")
            return redirect(url_for("main.bank_page", company_id=company_id))
        require_company_access(session, pending.company_id)
        company_id = pending.company_id
        try:
            result = submit_fints_tan(
                session=session,
                dialog_id=dialog_id,
                pin=request.form.get("pin") or "",
                tan=request.form.get("tan") or None,
                product_id=current_app.config.get("FINTS_PRODUCT_ID"),
                changed_by=changed_by(),
            )
        except FinTSSyncError as exc:
            flask_session.pop("fints_challenge", None)
            flash(str(exc), "error")
            return redirect(url_for("main.bank_page", company_id=company_id))

    return _handle_fints_result(result, company_id)


@main_bp.post("/bank/fints/tan/abbrechen")
def fints_tan_cancel_action():
    company_id = request.form.get("company_id", type=int)
    challenge = flask_session.pop("fints_challenge", None) or {}
    dialog_id = (request.form.get("dialog_id") or "").strip() or challenge.get("dialog_id")

    if dialog_id:
        session_factory = get_session_factory()
        with session_factory() as session:
            pending = session.get(FinTSPendingDialog, dialog_id)
            if pending is not None:
                require_company_access(session, pending.company_id)
                company_id = pending.company_id
                cancel_pending_dialog(
                    session=session, dialog_id=dialog_id, changed_by=changed_by()
                )

    flash("TAN-Dialog wurde verworfen.", "success")
    return redirect(url_for("main.bank_page", company_id=company_id))
