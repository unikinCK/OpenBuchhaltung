"""Kontenverwaltung: Kontenliste, Konto anlegen und Kontenrahmen-Prüfung."""

from __future__ import annotations

from datetime import date
from io import StringIO

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.services.account_chart_check import check_account_chart
from app.services.account_chart_import import (
    BUNDLED_ACCOUNT_CHART_FILES,
    import_account_chart_csv,
    import_bundled_account_chart,
)
from app.services.account_ledger import (
    DEFAULT_LEDGER_LIMIT,
    MAX_LEDGER_LIMIT,
    AccountLedgerError,
    account_ledger,
)
from app.services.accounts import (
    ACCOUNT_TYPES,
    AccountUpdateError,
    account_history,
    create_account_with_audit,
    update_account_master_data,
)
from app.services.audit_log import list_audit_log_entries
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    pagination_args,
    require_company_access,
    search_args,
)
from domain.models import Account, FiscalYear, JournalEntry, JournalEntryLine

# Auswahl „Gesamter Zeitraum“ im Kontoblatt (statt eines Geschäftsjahres).
ALL_FISCAL_YEARS = "alle"


@main_bp.get("/konten")
def accounts_page():
    query = (request.args.get("q") or "").strip() or None
    type_filter = (request.args.get("account_type") or "").strip() or None
    status_filter = (request.args.get("status") or "").strip() or None
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        accounts = []
        all_accounts_count = 0
        if selected_company_id:
            accounts_stmt = scoped_select(Account, company_id=selected_company_id)
            all_accounts_count = session.execute(
                select(func.count()).select_from(accounts_stmt.subquery())
            ).scalar_one()
            if query:
                pattern = f"%{query}%"
                accounts_stmt = accounts_stmt.where(
                    Account.code.ilike(pattern) | Account.name.ilike(pattern)
                )
            if type_filter:
                accounts_stmt = accounts_stmt.where(Account.account_type == type_filter)
            if status_filter in {"aktiv", "inaktiv"}:
                accounts_stmt = accounts_stmt.where(
                    Account.is_active.is_(status_filter == "aktiv")
                )
            accounts = session.execute(accounts_stmt.order_by(Account.code)).scalars().all()
        account_events = (
            list_audit_log_entries(
                session=session,
                company_id=selected_company_id,
                entity_type="account",
                limit=100,
            )
            if selected_company_id
            else []
        )
        chart_check = (
            check_account_chart(session=session, company_id=selected_company_id)
            if selected_company_id
            else None
        )

    return render_template(
        "konten.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        all_accounts_count=all_accounts_count,
        q=query,
        type_filter=type_filter,
        status_filter=status_filter,
        filters_active=bool(query or type_filter or status_filter),
        account_events=account_events,
        chart_check=chart_check,
        bundled_charts=sorted(BUNDLED_ACCOUNT_CHART_FILES),
        valid_account_types=ACCOUNT_TYPES,
    )


def _default_fiscal_year(fiscal_years: list[FiscalYear]) -> FiscalYear | None:
    """Laufendes Geschäftsjahr, sonst das jüngste."""
    today = date.today()
    for fiscal_year in fiscal_years:
        if fiscal_year.start_date <= today <= fiscal_year.end_date:
            return fiscal_year
    return fiscal_years[0] if fiscal_years else None


@main_bp.get("/konten/<int:account_id>")
def account_detail_page(account_id: int):
    """Kontoblatt: Buchungen eines Kontos mit Anfangs- und laufendem Saldo."""
    limit, offset = pagination_args(default=DEFAULT_LEDGER_LIMIT, maximum=MAX_LEDGER_LIMIT)
    _, date_from, date_to = search_args()
    fiscal_year_raw = (request.args.get("fiscal_year_id") or "").strip()
    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, account_id)
        if account is None:
            abort(404)
        require_company_access(session, account.company_id)
        companies, _ = company_context(session)
        fiscal_years = (
            session.execute(
                select(FiscalYear)
                .where(FiscalYear.company_id == account.company_id)
                .order_by(FiscalYear.start_date.desc())
            )
            .scalars()
            .all()
        )
        fiscal_year_id = None
        if fiscal_year_raw.isdigit():
            fiscal_year_id = int(fiscal_year_raw)
        elif fiscal_year_raw != ALL_FISCAL_YEARS and not (date_from or date_to):
            default_year = _default_fiscal_year(fiscal_years)
            fiscal_year_id = default_year.id if default_year else None
        try:
            ledger = account_ledger(
                session=session,
                account=account,
                date_from=date_from,
                date_to=date_to,
                fiscal_year_id=fiscal_year_id,
                limit=limit,
                offset=offset,
            )
        except AccountLedgerError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.account_detail_page", account_id=account.id))
        posting_count = session.execute(
            select(func.count(JournalEntryLine.id))
            .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
            .where(
                JournalEntryLine.account_id == account.id,
                JournalEntry.company_id == account.company_id,
            )
        ).scalar_one()
        history = account_history(session=session, account_id=account.id, limit=100)

    filter_args = {
        key: value
        for key, value in (
            ("fiscal_year_id", fiscal_year_id or (ALL_FISCAL_YEARS if fiscal_year_raw else None)),
            ("date_from", date_from.isoformat() if date_from else None),
            ("date_to", date_to.isoformat() if date_to else None),
        )
        if value
    }
    return render_template(
        "konto_detail.html",
        companies=companies,
        selected_company_id=account.company_id,
        account=account,
        ledger=ledger,
        fiscal_years=fiscal_years,
        fiscal_year_id=fiscal_year_id,
        all_fiscal_years=ALL_FISCAL_YEARS,
        explicit_date_from=date_from,
        explicit_date_to=date_to,
        posting_count=posting_count,
        history=history,
        filter_args=filter_args,
        limit=limit,
        offset=offset,
        valid_account_types=ACCOUNT_TYPES,
    )


@main_bp.post("/accounts")
def create_account():
    company_id = request.form.get("company_id", type=int)
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    account_type = request.form.get("account_type", "").strip()

    if not company_id or not code or not name or not account_type:
        flash("Alle Felder für das Konto müssen ausgefüllt sein.", "error")
        return redirect(url_for("main.accounts_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            create_account_with_audit(
                session=session,
                company=company,
                code=code,
                name=name,
                account_type=account_type,
                subledger=request.form.get("subledger") or None,
                changed_by=changed_by(),
            )
            session.commit()
        except AccountUpdateError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))
        except IntegrityError:
            session.rollback()
            flash("Konto mit dieser Nummer existiert bereits.", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash("Konto wurde angelegt.", "success")
    return redirect(url_for("main.accounts_page", company_id=company_id))


@main_bp.post("/accounts/<int:account_id>/update")
def update_account(account_id: int):
    name = request.form.get("name", "").strip()
    active_raw = request.form.get("is_active", "").strip().lower()
    if active_raw not in {"true", "false"}:
        flash("Ungültiger Kontostatus.", "error")
        return redirect(url_for("main.accounts_page"))

    session_factory = get_session_factory()
    with session_factory() as session:
        account = session.get(Account, account_id)
        if account is None:
            abort(404)
        require_company_access(session, account.company_id)
        extra_changes = {}
        if "subledger" in request.form:
            extra_changes["subledger"] = request.form.get("subledger") or None
        if request.form.get("account_type"):
            extra_changes["account_type"] = request.form["account_type"]
        try:
            changed = update_account_master_data(
                session=session,
                account=account,
                changed_by=changed_by(),
                name=name,
                is_active=active_raw == "true",
                **extra_changes,
            )
        except AccountUpdateError as exc:
            flash(str(exc), "error")
            if request.form.get("next") == "detail":
                return redirect(url_for("main.account_detail_page", account_id=account_id))
            return redirect(url_for("main.accounts_page", company_id=account.company_id))
        session.commit()
        company_id = account.company_id

    flash(
        "Konto wurde geändert." if changed else "Keine Kontoänderung erkannt.",
        "success",
    )
    if request.form.get("next") == "detail":
        return redirect(url_for("main.account_detail_page", account_id=account_id))
    return redirect(url_for("main.accounts_page", company_id=company_id))


@main_bp.post("/accounts/import-chart")
def import_account_chart_action():
    company_id = request.form.get("company_id", type=int)
    chart = (request.form.get("chart") or "").strip().lower()
    uploaded_file = request.files.get("account_chart_csv")
    has_chart = bool(chart)
    has_upload = uploaded_file is not None and bool(uploaded_file.filename)

    if not company_id:
        flash("Bitte zuerst eine Gesellschaft auswählen.", "error")
        return redirect(url_for("main.accounts_page"))
    if has_chart == has_upload:
        flash("Bitte entweder SKR03/SKR04 wählen oder eine CSV-Datei hochladen.", "error")
        return redirect(url_for("main.accounts_page", company_id=company_id))
    if has_chart and chart not in BUNDLED_ACCOUNT_CHART_FILES:
        flash("Unbekannter Kontenrahmen.", "error")
        return redirect(url_for("main.accounts_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            if has_chart:
                report = import_bundled_account_chart(
                    session=session,
                    company_id=company.id,
                    chart=chart,
                    changed_by=changed_by(),
                )
            else:
                csv_text = uploaded_file.read().decode("utf-8-sig")
                report = import_account_chart_csv(
                    session=session,
                    company_id=company.id,
                    csv_stream=StringIO(csv_text),
                    changed_by=changed_by(),
                )
        except (UnicodeDecodeError, ValueError) as exc:
            flash(f"Kontenrahmen-Import fehlgeschlagen: {exc}", "error")
            return redirect(url_for("main.accounts_page", company_id=company_id))

    flash(
        f"Kontenrahmen-Import: {report.imported_rows} neu, "
        f"{report.duplicate_rows} Duplikate, {report.error_rows} Fehler.",
        "success" if report.error_rows == 0 else "error",
    )
    return redirect(url_for("main.accounts_page", company_id=company_id))
