"""Kassenbuch: Kassen, Einträge, Kassensturz, Kassenbericht und Prüfung (ADR-007)."""

from __future__ import annotations

from datetime import date

from flask import abort, flash, make_response, redirect, render_template, request, url_for

from app.auth import WRITE_ROLES, current_user
from app.services.cash_book import (
    ENTRY_TYPE_LABELS,
    CashBookError,
    account_balance,
    business_today,
    cash_report,
    cash_report_csv,
    cash_report_file_name,
    create_cash_book,
    create_cash_closing,
    create_cash_entry,
    current_balance,
    last_closing,
    last_entry,
    list_cash_books,
    list_cash_closings,
    list_cash_entries,
    reversal_numbers,
    reverse_cash_entry,
    update_cash_book,
    verify_cash_book_integrity,
)
from app.services.journal_entries import JournalEntryCreationError
from app.services.scoping import scoped_select
from app.services.standard_accounts import KASSE, standard_account_id
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    pagination_args,
    parse_form_amount,
    require_company_access,
    search_args,
)
from domain.models import Account, BusinessPartner, CashBook, Document, TaxCode

# So viele unverknüpfte Belege bietet das Erfassungsformular an (neueste zuerst).
DOCUMENT_CHOICES_LIMIT = 100
CLOSING_LIST_LIMIT = 30


def _can_write() -> bool:
    user = current_user()
    return bool(user and user["role"] in WRITE_ROLES)


def _book_or_404(session, cash_book_id: int) -> CashBook:
    book = session.get(CashBook, cash_book_id)
    if book is None:
        abort(404)
    require_company_access(session, book.company_id)
    return book


def _form_date(field: str) -> date | None:
    raw = (request.form.get(field) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise CashBookError("Bitte ein gültiges Datum angeben.") from None


def _form_amount(field: str) -> object | None:
    raw = (request.form.get(field) or "").strip()
    return parse_form_amount(raw) if raw else None


def _book_url(book_id: int, company_id: int):
    return redirect(url_for("main.cash_book_page", company_id=company_id, cash_book_id=book_id))


def _company_choices(session, company_id: int) -> dict:
    accounts = (
        session.execute(
            scoped_select(Account, company_id=company_id)
            .where(Account.is_active.is_(True))
            .order_by(Account.code)
        )
        .scalars()
        .all()
    )
    tax_codes = (
        session.execute(
            scoped_select(TaxCode, company_id=company_id)
            .where(TaxCode.is_active.is_(True))
            .order_by(TaxCode.code)
        )
        .scalars()
        .all()
    )
    partners = (
        session.execute(
            scoped_select(BusinessPartner, company_id=company_id)
            .where(BusinessPartner.is_active.is_(True))
            .order_by(BusinessPartner.name)
        )
        .scalars()
        .all()
    )
    documents = (
        session.execute(
            scoped_select(Document, company_id=company_id)
            .where(Document.journal_entry_id.is_(None), Document.is_current.is_(True))
            .order_by(Document.uploaded_at.desc())
            .limit(DOCUMENT_CHOICES_LIMIT)
        )
        .scalars()
        .all()
    )
    return {
        "accounts": accounts,
        "tax_codes": tax_codes,
        "partners": partners,
        "documents": documents,
    }


@main_bp.get("/kassenbuch")
def cash_book_page():
    limit, offset = pagination_args()
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        context: dict = {
            "books": [],
            "selected_book": None,
            "entries": [],
            "entries_total": 0,
            "closings": [],
            "accounts": [],
            "free_cash_accounts": [],
            "default_cash_account_id": None,
            "tax_codes": [],
            "partners": [],
            "documents": [],
            "reversed_by": {},
        }
        if selected_company_id:
            books = list_cash_books(
                session=session, company_id=selected_company_id, include_inactive=True
            )
            choices = _company_choices(session, selected_company_id)
            used_accounts = {book.account_id for book in books}
            free_cash_accounts = [
                account
                for account in choices["accounts"]
                if account.account_type == "asset"
                and account.subledger is None
                and account.id not in used_accounts
            ]
            requested_id = request.args.get("cash_book_id", type=int)
            selected_book = next((book for book in books if book.id == requested_id), None)
            if selected_book is None and books:
                selected_book = next((book for book in books if book.is_active), books[0])
            context.update(choices)
            context.update(
                {
                    "free_cash_accounts": free_cash_accounts,
                    "default_cash_account_id": standard_account_id(free_cash_accounts, KASSE),
                    "books": [
                        {
                            "book": book,
                            "balance": current_balance(session, book.id),
                            "last_closing": last_closing(session, book.id),
                        }
                        for book in books
                    ],
                    "selected_book": selected_book,
                }
            )
            if selected_book is not None:
                entries, total = list_cash_entries(
                    session=session, cash_book_id=selected_book.id, limit=limit, offset=offset
                )
                closings, _ = list_cash_closings(
                    session=session, cash_book_id=selected_book.id, limit=CLOSING_LIST_LIMIT
                )
                balance = current_balance(session, selected_book.id)
                on_account = account_balance(session, selected_book.account_id)
                context.update(
                    {
                        "entries": entries,
                        "entries_total": total,
                        "closings": closings,
                        "balance": balance,
                        "account_balance": on_account,
                        "last_entry": last_entry(session, selected_book.id),
                        "last_closing": last_closing(session, selected_book.id),
                        "reversed_by": reversal_numbers(session, [e.id for e in entries]),
                    }
                )
        return render_template(
            "kassenbuch.html",
            companies=companies,
            selected_company_id=selected_company_id,
            today=business_today().isoformat(),
            limit=limit,
            offset=offset,
            entry_type_labels=ENTRY_TYPE_LABELS,
            can_write=_can_write(),
            **context,
        )


@main_bp.post("/kassenbuch/anlegen")
def create_cash_book_action():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(400)
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            book = create_cash_book(
                session=session,
                company_id=company_id,
                account_id=request.form.get("account_id", type=int),
                name=request.form.get("name"),
                opening_amount=_form_amount("opening_amount"),
                opening_date=_form_date("opening_date"),
                opening_contra_account_id=request.form.get(
                    "opening_contra_account_id", type=int
                ),
                changed_by=changed_by(),
            )
        except (CashBookError, JournalEntryCreationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.cash_book_page", company_id=company_id))
        flash(f"Kasse {book.name} wurde angelegt.", "success")
        return _book_url(book.id, company_id)


@main_bp.post("/kassenbuch/<int:cash_book_id>/aendern")
def update_cash_book_action(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        raw_active = request.form.get("is_active")
        try:
            update_cash_book(
                session=session,
                cash_book_id=cash_book_id,
                name=request.form.get("name"),
                is_active=None if raw_active is None else raw_active == "1",
                changed_by=changed_by(),
            )
        except CashBookError as exc:
            flash(str(exc), "error")
        else:
            flash("Kasse wurde geändert.", "success")
    return _book_url(cash_book_id, company_id)


@main_bp.post("/kassenbuch/<int:cash_book_id>/eintrag")
def create_cash_entry_action(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        try:
            entry = create_cash_entry(
                session=session,
                cash_book_id=cash_book_id,
                entry_type=request.form.get("entry_type"),
                entry_date=_form_date("entry_date"),
                amount=_form_amount("amount"),
                description=request.form.get("description"),
                contra_account_id=request.form.get("contra_account_id", type=int),
                tax_code_id=request.form.get("tax_code_id", type=int),
                partner_id=request.form.get("partner_id", type=int),
                document_id=request.form.get("document_id", type=int),
                changed_by=changed_by(),
            )
        except (CashBookError, JournalEntryCreationError) as exc:
            flash(str(exc), "error")
        else:
            flash(f"Eintrag Nr. {entry.entry_number} wurde erfasst und gebucht.", "success")
    return _book_url(cash_book_id, company_id)


@main_bp.post("/kassenbuch/<int:cash_book_id>/eintrag/<int:entry_id>/storno")
def reverse_cash_entry_action(cash_book_id: int, entry_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        try:
            entry = reverse_cash_entry(
                session=session,
                cash_book_id=cash_book_id,
                entry_id=entry_id,
                changed_by=changed_by(),
            )
        except CashBookError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Storno erfasst als Eintrag Nr. {entry.entry_number}.", "success")
    return _book_url(cash_book_id, company_id)


@main_bp.post("/kassenbuch/<int:cash_book_id>/abschluss")
def create_cash_closing_action(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        try:
            closing = create_cash_closing(
                session=session,
                cash_book_id=cash_book_id,
                closing_date=_form_date("closing_date"),
                counted_balance=_form_amount("counted_balance"),
                difference_account_id=request.form.get("difference_account_id", type=int),
                description=request.form.get("description"),
                changed_by=changed_by(),
            )
        except (CashBookError, JournalEntryCreationError) as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"Kassensturz vom {closing.closing_date.strftime('%d.%m.%Y')} ist erfasst, "
                "die Tage bis dahin sind abgeschlossen.",
                "success",
            )
    return _book_url(cash_book_id, company_id)


def _report_for_request(session, book: CashBook):
    """Kassenbericht zum Zeitraum der Anfrage; ohne Angabe der laufende Monat."""
    _, date_from, date_to = search_args()
    if "date_from" not in request.args and "date_to" not in request.args:
        date_to = business_today()
        date_from = date_to.replace(day=1)
    return cash_report(
        session=session, cash_book_id=book.id, date_from=date_from, date_to=date_to
    )


@main_bp.get("/kassenbuch/<int:cash_book_id>/bericht")
def cash_report_page(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        try:
            report = _report_for_request(session, book)
        except CashBookError as exc:
            flash(str(exc), "error")
            return _book_url(cash_book_id, company_id)
        return render_template(
            "kassenbuch_bericht.html",
            report=report,
            company=book.account.company,
            printed_on=business_today(),
            entry_type_labels=ENTRY_TYPE_LABELS,
        )


@main_bp.get("/kassenbuch/<int:cash_book_id>/bericht.csv")
def cash_report_csv_download(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        company_id = book.company_id
        try:
            report = _report_for_request(session, book)
        except CashBookError as exc:
            flash(str(exc), "error")
            return _book_url(cash_book_id, company_id)
        response = make_response(cash_report_csv(report))
        file_name = cash_report_file_name(report)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
    return response


@main_bp.get("/kassenbuch/<int:cash_book_id>/pruefung")
def cash_book_integrity_page(cash_book_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        book = _book_or_404(session, cash_book_id)
        companies, _ = company_context(session)
        result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
        return render_template(
            "kassenbuch_pruefung.html",
            companies=companies,
            selected_company_id=book.company_id,
            book=book,
            result=result,
        )
