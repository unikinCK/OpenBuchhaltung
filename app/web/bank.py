"""Bankumsätze: CSV-Import, Matching und Verbuchung."""

from __future__ import annotations

from io import StringIO

from flask import abort, flash, redirect, render_template, request, url_for

from app.services.bank_import import (
    BankImportError,
    book_transaction,
    import_bank_csv,
    match_transaction,
    suggest_matches,
)
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, BankTransaction, ControllingUnit, JournalEntry, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/bank")
def bank_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        bank_accounts = []
        contra_accounts = []
        tax_codes = []
        transactions = []
        cost_centers = []
        profit_centers = []
        suggestions_by_tx: dict[int, list[JournalEntry]] = {}
        if selected_company_id:
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
            transactions = (
                session.execute(
                    scoped_select(BankTransaction, company_id=selected_company_id).order_by(
                        BankTransaction.booking_date.desc(), BankTransaction.id.desc()
                    )
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
            for transaction in transactions:
                if transaction.status == "open":
                    suggestions_by_tx[transaction.id] = suggest_matches(
                        session=session, transaction=transaction
                    )

    return render_template(
        "bank.html",
        companies=companies,
        selected_company_id=selected_company_id,
        bank_accounts=bank_accounts,
        contra_accounts=contra_accounts,
        tax_codes=tax_codes,
        transactions=transactions,
        suggestions_by_tx=suggestions_by_tx,
        cost_centers=cost_centers,
        profit_centers=profit_centers,
    )


@main_bp.post("/bank/import")
def bank_import_action():
    company_id = request.form.get("company_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    uploaded_file = request.files.get("bank_csv")

    if not company_id or not bank_account_id or uploaded_file is None or not uploaded_file.filename:
        flash("Gesellschaft, Bankkonto und CSV-Datei sind Pflichtfelder.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            text_stream = StringIO(uploaded_file.read().decode("utf-8-sig"))
            report = import_bank_csv(
                session=session,
                company_id=company_id,
                bank_account_id=bank_account_id,
                csv_stream=text_stream,
                changed_by=changed_by(),
            )
        except (BankImportError, UnicodeDecodeError) as exc:
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
