"""Offene Posten: Übersicht, Anlage und Ausgleich."""

from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for

from app.services.journal_entries import parse_decimal
from app.services.open_items import (
    OpenItemError,
    OpenItemInput,
    create_open_item,
    list_open_items,
    settle_open_item,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, BankTransaction, JournalEntry, OpenItem


@main_bp.get("/offene-posten")
def open_items_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        accounts = []
        journal_entries = []
        bank_transactions = []
        open_items = []
        include_settled = request.args.get("include_settled") == "1"
        totals = {"receivable": parse_decimal("0.00"), "payable": parse_decimal("0.00")}
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
            journal_entries = (
                session.execute(
                    scoped_select(JournalEntry, company_id=selected_company_id).order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                )
                .scalars()
                .all()
            )
            bank_transactions = (
                session.execute(
                    scoped_select(BankTransaction, company_id=selected_company_id).order_by(
                        BankTransaction.booking_date.desc(), BankTransaction.id.desc()
                    )
                )
                .scalars()
                .all()
            )
            open_items = list_open_items(
                session=session,
                company_id=selected_company_id,
                include_settled=include_settled,
            )
            for item in open_items:
                if item.status == "open":
                    totals[item.item_type] += item.open_amount

    return render_template(
        "offene_posten.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        journal_entries=journal_entries,
        bank_transactions=bank_transactions,
        open_items=open_items,
        include_settled=include_settled,
        totals=totals,
        today=date.today().isoformat(),
    )


@main_bp.post("/offene-posten")
def create_open_item_action():
    company_id = request.form.get("company_id", type=int)
    account_id = request.form.get("account_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)
    item_type = request.form.get("item_type", "").strip()
    reference = request.form.get("reference", "").strip()
    counterparty = request.form.get("counterparty", "").strip() or None
    entry_date_raw = request.form.get("entry_date", "").strip()
    due_date_raw = request.form.get("due_date", "").strip()
    amount_raw = request.form.get("amount", "").strip()

    if not company_id or not account_id or not item_type or not reference or not amount_raw:
        flash("Gesellschaft, Konto, Typ, Referenz und Betrag sind Pflichtfelder.", "error")
        return redirect(url_for("main.open_items_page", company_id=company_id))

    try:
        entry_date = date.fromisoformat(entry_date_raw) if entry_date_raw else date.today()
        due_date = date.fromisoformat(due_date_raw) if due_date_raw else None
        amount = parse_decimal(amount_raw)
    except ValueError:
        flash("Datum oder Betrag ist ungültig.", "error")
        return redirect(url_for("main.open_items_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            item = create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company_id,
                    account_id=account_id,
                    journal_entry_id=journal_entry_id,
                    item_type=item_type,
                    reference=reference,
                    counterparty=counterparty,
                    entry_date=entry_date,
                    due_date=due_date,
                    amount=amount,
                    changed_by=changed_by(),
                ),
            )
        except OpenItemError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.open_items_page", company_id=company_id))

    flash(f"Offener Posten {item.reference} wurde angelegt.", "success")
    return redirect(url_for("main.open_items_page", company_id=company_id))


@main_bp.post("/offene-posten/<int:open_item_id>/ausgleichen")
def settle_open_item_action(open_item_id: int):
    company_id = request.form.get("company_id", type=int)
    amount_raw = request.form.get("amount", "").strip()
    bank_transaction_id = request.form.get("bank_transaction_id", type=int)
    journal_entry_id = request.form.get("journal_entry_id", type=int)

    try:
        amount = parse_decimal(amount_raw) if amount_raw else None
    except ValueError:
        flash("Ausgleichsbetrag ist ungültig.", "error")
        return redirect(url_for("main.open_items_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        item = session.get(OpenItem, open_item_id)
        if item is None:
            abort(404)
        require_company_access(session, item.company_id)
        company_id = item.company_id
        try:
            item = settle_open_item(
                session=session,
                open_item_id=open_item_id,
                amount=amount,
                bank_transaction_id=bank_transaction_id,
                journal_entry_id=journal_entry_id,
                changed_by=changed_by(),
            )
        except OpenItemError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.open_items_page", company_id=company_id))

    if item.status == "settled":
        flash(f"Offener Posten {item.reference} wurde ausgeglichen.", "success")
    else:
        flash(f"Teilzahlung erfasst. Offen: {item.open_amount}.", "success")
    return redirect(url_for("main.open_items_page", company_id=company_id))
