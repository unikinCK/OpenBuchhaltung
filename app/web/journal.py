"""Buchungsjournal: Übersicht und Erfassung von Journalbuchungen."""

from __future__ import annotations

from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entries_until,
    finalize_journal_entry,
    parse_decimal,
    reverse_journal_entry,
)
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Account, ControllingUnit, JournalEntry, JournalEntryLine, TaxCode
from domain.services.journal_entry_validation import JournalEntryValidationError


@main_bp.get("/buchungen")
def journal_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        accounts = []
        tax_codes = []
        cost_centers = []
        profit_centers = []
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
            cost_centers = (
                session.execute(
                    scoped_select(ControllingUnit, company_id=selected_company_id)
                    .where(
                        ControllingUnit.unit_type == "cost_center",
                        ControllingUnit.is_active.is_(True),
                    )
                    .order_by(ControllingUnit.code)
                )
                .scalars()
                .all()
            )
            profit_centers = (
                session.execute(
                    scoped_select(ControllingUnit, company_id=selected_company_id)
                    .where(
                        ControllingUnit.unit_type == "profit_center",
                        ControllingUnit.is_active.is_(True),
                    )
                    .order_by(ControllingUnit.code)
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
            cost_unit = aliased(ControllingUnit)
            profit_unit = aliased(ControllingUnit)
            line_rows = session.execute(
                select(
                    JournalEntryLine.journal_entry_id,
                    JournalEntryLine.line_number,
                    Account.code,
                    Account.name,
                    JournalEntryLine.debit_amount,
                    JournalEntryLine.credit_amount,
                    JournalEntryLine.description,
                    cost_unit.code.label("cost_center_code"),
                    profit_unit.code.label("profit_center_code"),
                )
                .join(Account, Account.id == JournalEntryLine.account_id)
                .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
                .outerjoin(cost_unit, cost_unit.id == JournalEntryLine.cost_center_id)
                .outerjoin(profit_unit, profit_unit.id == JournalEntryLine.profit_center_id)
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
                        "cost_center_code": row.cost_center_code,
                        "profit_center_code": row.profit_center_code,
                    }
                )

        # Storno-Verweise: Original-ID -> Belegnummer der Stornobuchung.
        reversed_by_entry = {
            entry.reversal_of_id: entry.posting_number
            for entry in journal_entries
            if entry.reversal_of_id is not None
        }
        posting_number_by_id = {entry.id: entry.posting_number for entry in journal_entries}

    return render_template(
        "buchungen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        accounts=accounts,
        tax_codes=tax_codes,
        cost_centers=cost_centers,
        profit_centers=profit_centers,
        journal_entries=journal_entries,
        lines_by_entry=lines_by_entry,
        reversed_by_entry=reversed_by_entry,
        posting_number_by_id=posting_number_by_id,
        today=date.today().isoformat(),
    )


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
        line_cost_center_ids = request.form.getlist("line_cost_center_id")
        line_profit_center_ids = request.form.getlist("line_profit_center_id")

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
                cost_center_raw = (
                    line_cost_center_ids[idx] if idx < len(line_cost_center_ids) else ""
                ).strip()
                profit_center_raw = (
                    line_profit_center_ids[idx] if idx < len(line_profit_center_ids) else ""
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
                        cost_center_id=(int(cost_center_raw) if cost_center_raw else None),
                        profit_center_id=(int(profit_center_raw) if profit_center_raw else None),
                    )
                )

            if len(line_inputs) < 2:
                raise JournalEntryCreationError("Bitte mindestens zwei Buchungszeilen erfassen.")

        entry_payload = JournalEntryInput(
            company_id=company_id,
            entry_date=parsed_date,
            description=description,
            status="posted",
            changed_by=changed_by(),
            lines=line_inputs,
            post_to_closing_period=bool(request.form.get("post_to_closing_period")),
        )

        session_factory = get_session_factory()
        with session_factory() as session:
            require_company_access(session, company_id)
            entry = create_journal_entry(session=session, payload=entry_payload)

    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.journal_page", company_id=company_id))
    except IntegrityError:
        flash("Die Buchung konnte nicht gespeichert werden. Bitte erneut versuchen.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    flash(f"Buchung {entry.posting_number} wurde gespeichert.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/journal-entries/<int:journal_entry_id>/festschreiben")
def finalize_journal_entry_action(journal_entry_id: int):
    company_id = request.form.get("company_id", type=int)

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = session.get(JournalEntry, journal_entry_id)
        if entry is None:
            abort(404)
        require_company_access(session, entry.company_id)
        company_id = entry.company_id
        try:
            entry = finalize_journal_entry(
                session=session, journal_entry_id=journal_entry_id, changed_by=changed_by()
            )
        except JournalEntryCreationError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.journal_page", company_id=company_id))

    flash(f"Buchung {entry.posting_number} wurde festgeschrieben.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/journal-entries/festschreiben")
def finalize_journal_entries_action():
    company_id = request.form.get("company_id", type=int)
    up_to_raw = request.form.get("up_to_date", "").strip()

    if not company_id or not up_to_raw:
        flash("Gesellschaft und Datum sind Pflichtfelder.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    try:
        up_to_date = date.fromisoformat(up_to_raw)
    except ValueError:
        flash("Ungültiges Datum für den Festschreibelauf.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        count = finalize_journal_entries_until(
            session=session,
            company_id=company_id,
            up_to_date=up_to_date,
            changed_by=changed_by(),
        )

    if count:
        flash(
            f"{count} Buchung(en) bis {up_to_date.isoformat()} wurden festgeschrieben.",
            "success",
        )
    else:
        flash("Keine offenen Buchungen bis zu diesem Datum gefunden.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/journal-entries/<int:journal_entry_id>/stornieren")
def reverse_journal_entry_action(journal_entry_id: int):
    company_id = request.form.get("company_id", type=int)
    reversal_date_raw = request.form.get("reversal_date", "").strip()

    try:
        reversal_date = (
            date.fromisoformat(reversal_date_raw) if reversal_date_raw else date.today()
        )
    except ValueError:
        flash("Ungültiges Stornodatum.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id))

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = session.get(JournalEntry, journal_entry_id)
        if entry is None:
            abort(404)
        require_company_access(session, entry.company_id)
        company_id = entry.company_id
        original_number = entry.posting_number
        try:
            reversal = reverse_journal_entry(
                session=session,
                journal_entry_id=journal_entry_id,
                reversal_date=reversal_date,
                changed_by=changed_by(),
            )
        except (JournalEntryCreationError, JournalEntryValidationError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.journal_page", company_id=company_id))

    flash(
        f"Buchung {original_number} wurde storniert (Stornobuchung {reversal.posting_number}).",
        "success",
    )
    return redirect(url_for("main.journal_page", company_id=company_id))
