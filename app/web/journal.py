"""Buchungsjournal: Übersicht, Detailansicht und Erfassung von Journalbuchungen."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.services.audit_log import serialize_audit_log_entry
from app.services.documents import documents_by_journal_entry
from app.services.journal_entries import (
    CARRYFORWARD_SOURCES,
    KEEP_PARTNER,
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    amend_journal_entry,
    apply_entry_partner,
    create_journal_entry,
    finalize_journal_entries_until,
    finalize_journal_entry,
    parse_decimal,
    reverse_journal_entry,
    tax_account_ids,
)
from app.services.journal_templates import (
    JournalTemplateError,
    book_template,
    create_template,
    due_templates,
    list_templates,
    set_template_active,
)
from app.services.partners import partner_display_number
from app.services.scoping import scoped_select
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    filter_url_args,
    get_session_factory,
    pagination_args,
    parse_form_amount,
    require_company_access,
    search_args,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    BusinessPartner,
    ControllingUnit,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError

# Herkunft systemerzeugter Buchungen als Kennzeichen im Journal.
SOURCE_LABELS = {
    "storno": "Storno",
    "year_end_close": "Jahresabschluss",
    "carryforward": "Saldovortrag",
    "payroll": "Lohn",
}
STATUS_FILTERS = {"offen": False, "festgeschrieben": True}


def _form_context(session, company_id: int) -> dict[str, object]:
    """Stammdaten für Erfassungsmaske, Filter und Partnerzuordnung."""

    def active_units(unit_type: str):
        return (
            session.execute(
                scoped_select(ControllingUnit, company_id=company_id)
                .where(
                    ControllingUnit.unit_type == unit_type,
                    ControllingUnit.is_active.is_(True),
                )
                .order_by(ControllingUnit.code)
            )
            .scalars()
            .all()
        )

    return {
        "accounts": session.execute(
            scoped_select(Account, company_id=company_id).order_by(Account.code)
        )
        .scalars()
        .all(),
        "tax_codes": session.execute(
            scoped_select(TaxCode, company_id=company_id)
            .where(TaxCode.is_active.is_(True))
            .order_by(TaxCode.code)
        )
        .scalars()
        .all(),
        "cost_centers": active_units("cost_center"),
        "profit_centers": active_units("profit_center"),
        "partners": session.execute(
            scoped_select(BusinessPartner, company_id=company_id)
            .where(BusinessPartner.is_active.is_(True))
            .order_by(BusinessPartner.name)
        )
        .scalars()
        .all(),
    }


def entry_overview(session, company_id: int, entry_ids: list[int]) -> dict[int, dict]:
    """Zeilen, Soll-/Habenkonten, Betrag, Partner und Bankumsatz je Buchung."""
    overview: dict[int, dict] = {
        entry_id: {
            "lines": [],
            "debit_lines": [],
            "credit_lines": [],
            "total": Decimal("0.00"),
            "partners": [],
            "bank_transaction": None,
        }
        for entry_id in entry_ids
    }
    if not entry_ids:
        return overview
    cost_unit = aliased(ControllingUnit)
    profit_unit = aliased(ControllingUnit)
    line_rows = session.execute(
        select(
            JournalEntryLine.journal_entry_id,
            JournalEntryLine.line_number,
            JournalEntryLine.account_id,
            Account.code,
            Account.name,
            Account.subledger,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.description,
            TaxCode.code.label("tax_code"),
            cost_unit.code.label("cost_center_code"),
            profit_unit.code.label("profit_center_code"),
            JournalEntryLine.partner_id,
            BusinessPartner.debtor_number,
            BusinessPartner.creditor_number,
            BusinessPartner.name.label("partner_name"),
        )
        .join(Account, Account.id == JournalEntryLine.account_id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .outerjoin(TaxCode, TaxCode.id == JournalEntryLine.tax_code_id)
        .outerjoin(cost_unit, cost_unit.id == JournalEntryLine.cost_center_id)
        .outerjoin(profit_unit, profit_unit.id == JournalEntryLine.profit_center_id)
        .outerjoin(BusinessPartner, BusinessPartner.id == JournalEntryLine.partner_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntryLine.journal_entry_id.in_(entry_ids),
        )
        .order_by(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number)
    ).all()
    for row in line_rows:
        item = overview[row.journal_entry_id]
        partner_number = None
        if row.partner_id is not None:
            if row.subledger == "creditor":
                partner_number = row.creditor_number or row.debtor_number
            else:
                partner_number = row.debtor_number or row.creditor_number
        line = {
            "line_number": row.line_number,
            "account_id": row.account_id,
            "account_code": row.code,
            "account_name": row.name,
            "subledger": row.subledger,
            "debit_amount": row.debit_amount,
            "credit_amount": row.credit_amount,
            "description": row.description,
            "tax_code": row.tax_code,
            "cost_center_code": row.cost_center_code,
            "profit_center_code": row.profit_center_code,
            "partner_id": row.partner_id,
            "partner_number": partner_number,
            "partner_name": row.partner_name,
        }
        item["lines"].append(line)
        if row.debit_amount > 0:
            item["debit_lines"].append(line)
            item["total"] += row.debit_amount
        else:
            item["credit_lines"].append(line)
        if row.partner_id is not None and row.partner_id not in {
            partner["id"] for partner in item["partners"]
        }:
            item["partners"].append(
                {"id": row.partner_id, "name": row.partner_name, "number": partner_number}
            )
    for transaction in session.execute(
        select(BankTransaction)
        .where(BankTransaction.journal_entry_id.in_(entry_ids))
        .order_by(BankTransaction.booking_date, BankTransaction.id)
    ).scalars():
        overview[transaction.journal_entry_id]["bank_transaction"] = transaction
    return overview


def _reversal_links(session, entries: list[JournalEntry]) -> tuple[dict, dict]:
    """Storno-Verweise: Original → Stornobuchung und Belegnummern der Originale.

    Auch wenn die zugehörige Buchung auf einer anderen Seite steht.
    """
    shown_ids = [entry.id for entry in entries]
    reversed_by_entry: dict[int, dict] = {}
    if shown_ids:
        for reversal_of_id, reversal_id, posting_number in session.execute(
            select(
                JournalEntry.reversal_of_id, JournalEntry.id, JournalEntry.posting_number
            ).where(JournalEntry.reversal_of_id.in_(shown_ids))
        ).all():
            reversed_by_entry[reversal_of_id] = {"id": reversal_id, "number": posting_number}
    posting_number_by_id = {entry.id: entry.posting_number for entry in entries}
    referenced_ids = {
        entry.reversal_of_id for entry in entries if entry.reversal_of_id
    } - set(posting_number_by_id)
    if referenced_ids:
        posting_number_by_id.update(
            session.execute(
                select(JournalEntry.id, JournalEntry.posting_number).where(
                    JournalEntry.id.in_(referenced_ids)
                )
            ).all()
        )
    return reversed_by_entry, posting_number_by_id


def _is_amendable(entry: JournalEntry, reversed_by: object) -> bool:
    return (
        not entry.is_finalized
        and not reversed_by
        and entry.source not in CARRYFORWARD_SOURCES
    )


def _safe_next(default: str) -> str:
    """Rücksprung auf eine Buchungsseite (nur relative Pfade unter /buchungen)."""
    target = (request.form.get("next") or "").strip()
    if target.startswith("/buchungen") and "//" not in target and "\\" not in target:
        return target
    return default


@main_bp.get("/buchungen")
def journal_page():
    limit, offset = pagination_args()
    query, date_from, date_to = search_args()
    partner_filter = request.args.get("partner_id", type=int)
    account_filter = request.args.get("account_id", type=int)
    status_filter = (request.args.get("status") or "").strip()
    if status_filter not in STATUS_FILTERS:
        status_filter = ""
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)

        context: dict[str, object] = {
            "accounts": [],
            "tax_codes": [],
            "cost_centers": [],
            "profit_centers": [],
            "partners": [],
        }
        journal_entries: list[JournalEntry] = []
        journal_entries_total = 0
        all_entries_count = 0
        templates = []
        due_template_ids: set[int] = set()
        overview: dict[int, dict] = {}
        filter_partner = None
        filter_account = None
        if selected_company_id:
            context = _form_context(session, selected_company_id)
            templates = list_templates(session=session, company_id=selected_company_id)
            due_template_ids = {
                template.id
                for template in due_templates(session=session, company_id=selected_company_id)
            }
            base_stmt = scoped_select(JournalEntry, company_id=selected_company_id)
            all_entries_count = session.execute(
                select(func.count()).select_from(base_stmt.subquery())
            ).scalar_one()
            entries_stmt = base_stmt
            if query:
                pattern = f"%{query}%"
                entries_stmt = entries_stmt.where(
                    JournalEntry.description.ilike(pattern)
                    | JournalEntry.posting_number.ilike(pattern)
                )
            if date_from:
                entries_stmt = entries_stmt.where(JournalEntry.entry_date >= date_from)
            if date_to:
                entries_stmt = entries_stmt.where(JournalEntry.entry_date <= date_to)
            if partner_filter:
                filter_partner = session.get(BusinessPartner, partner_filter)
                if filter_partner is None or filter_partner.company_id != selected_company_id:
                    filter_partner = None
                    partner_filter = None
                else:
                    entries_stmt = entries_stmt.where(
                        JournalEntry.id.in_(
                            select(JournalEntryLine.journal_entry_id).where(
                                JournalEntryLine.partner_id == partner_filter
                            )
                        )
                    )
            if account_filter:
                filter_account = session.get(Account, account_filter)
                if filter_account is None or filter_account.company_id != selected_company_id:
                    filter_account = None
                    account_filter = None
                else:
                    entries_stmt = entries_stmt.where(
                        JournalEntry.id.in_(
                            select(JournalEntryLine.journal_entry_id).where(
                                JournalEntryLine.account_id == account_filter
                            )
                        )
                    )
            if status_filter:
                entries_stmt = entries_stmt.where(
                    JournalEntry.is_finalized.is_(STATUS_FILTERS[status_filter])
                )
            journal_entries_total = session.execute(
                select(func.count()).select_from(entries_stmt.subquery())
            ).scalar_one()
            journal_entries = (
                session.execute(
                    entries_stmt.order_by(
                        JournalEntry.entry_date.desc(), JournalEntry.id.desc()
                    )
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
            overview = entry_overview(
                session, selected_company_id, [entry.id for entry in journal_entries]
            )

        reversed_by_entry, posting_number_by_id = _reversal_links(session, journal_entries)
        documents_by_entry = documents_by_journal_entry(
            session, [entry.id for entry in journal_entries]
        )

    filter_args = filter_url_args(
        query,
        date_from,
        date_to,
        partner_id=partner_filter,
        account_id=account_filter,
        status=status_filter,
    )
    return render_template(
        "buchungen.html",
        companies=companies,
        selected_company_id=selected_company_id,
        **context,
        journal_entries=journal_entries,
        journal_entries_total=journal_entries_total,
        all_entries_count=all_entries_count,
        templates=templates,
        due_template_ids=due_template_ids,
        limit=limit,
        offset=offset,
        q=query,
        date_from=date_from,
        date_to=date_to,
        partner_filter=partner_filter,
        filter_partner=filter_partner,
        account_filter=account_filter,
        filter_account=filter_account,
        status_filter=status_filter,
        filter_args=filter_args,
        filters_active=bool(filter_args),
        overview=overview,
        documents_by_entry=documents_by_entry,
        reversed_by_entry=reversed_by_entry,
        posting_number_by_id=posting_number_by_id,
        source_labels=SOURCE_LABELS,
        is_amendable=_is_amendable,
        open_form=request.args.get("neu") == "1",
        today=date.today().isoformat(),
    )


@main_bp.post("/buchungsvorlagen/<int:template_id>/buchen")
def book_journal_template_action(template_id: int):
    from domain.models import JournalTemplate

    session_factory = get_session_factory()
    with session_factory() as session:
        template = session.get(JournalTemplate, template_id)
        if template is None:
            abort(404)
        require_company_access(session, template.company_id)
        company_id = template.company_id
        try:
            entry, template = book_template(
                session=session, template_id=template_id, changed_by=changed_by()
            )
        except (
            JournalTemplateError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.journal_page", company_id=company_id))

    next_note = (
        f" Nächste Fälligkeit: {template.next_run.strftime('%d.%m.%Y')}."
        if template.next_run
        else ""
    )
    flash(
        f"Vorlage „{template.name}“ als Buchung {entry.posting_number} gebucht.{next_note}",
        "success",
    )
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/buchungsvorlagen/<int:template_id>/deaktivieren")
def deactivate_journal_template_action(template_id: int):
    from domain.models import JournalTemplate

    session_factory = get_session_factory()
    with session_factory() as session:
        template = session.get(JournalTemplate, template_id)
        if template is None:
            abort(404)
        require_company_access(session, template.company_id)
        company_id = template.company_id
        set_template_active(
            session=session, template_id=template_id, is_active=False, changed_by=changed_by()
        )

    flash("Vorlage wurde deaktiviert.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/journal-entries")
def create_journal_entry_from_form():
    company_id = request.form.get("company_id", type=int)
    entry_date_raw = request.form.get("entry_date", "").strip()
    description = request.form.get("description", "").strip()
    if not company_id or not entry_date_raw or not description:
        flash("Gesellschaft, Datum und Beschreibung sind Pflichtfelder.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id, neu=1))

    service_date_raw = request.form.get("service_date", "").strip()
    try:
        parsed_date = date.fromisoformat(entry_date_raw)
        service_date = date.fromisoformat(service_date_raw) if service_date_raw else None
    except ValueError:
        flash("Ungültiges Datum.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id, neu=1))
    entry_partner_id = request.form.get("partner_id", type=int)

    try:
        line_inputs: list[JournalLineInput] = []
        line_account_ids = request.form.getlist("line_account_id")
        line_sides = request.form.getlist("line_side")
        line_amounts = request.form.getlist("line_amount")
        line_descriptions = request.form.getlist("line_description")
        line_tax_code_ids = request.form.getlist("line_tax_code_id")
        line_cost_center_ids = request.form.getlist("line_cost_center_id")
        line_profit_center_ids = request.form.getlist("line_profit_center_id")
        line_partner_ids = request.form.getlist("line_partner_id")

        # Backward-compatible fallback (legacy single amount + Soll/Haben Felder)
        if not line_account_ids:
            debit_account_id = request.form.get("debit_account_id", type=int)
            credit_account_id = request.form.get("credit_account_id", type=int)
            amount_raw = request.form.get("amount", "").strip()
            if not debit_account_id or not credit_account_id:
                flash("Bitte Soll- und Habenkonto auswählen.", "error")
                return redirect(url_for("main.journal_page", company_id=company_id, neu=1))
            amount = parse_form_amount(amount_raw)
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
                partner_raw = (
                    line_partner_ids[idx] if idx < len(line_partner_ids) else ""
                ).strip()
                if not account_raw and not amount_raw:
                    continue
                if not account_raw or not amount_raw or side_raw not in {"debit", "credit"}:
                    raise JournalEntryCreationError(
                        f"Zeile {idx + 1}: Konto, Seite (Soll/Haben) und Betrag sind erforderlich."
                    )
                amount = parse_form_amount(amount_raw)
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
                        partner_id=int(partner_raw) if partner_raw else None,
                    )
                )

            if len(line_inputs) < 2:
                raise JournalEntryCreationError("Bitte mindestens zwei Buchungszeilen erfassen.")

        session_factory = get_session_factory()
        with session_factory() as session:
            require_company_access(session, company_id)
            if entry_partner_id:
                # Partner der Buchung auf alle Zeilen ohne eigenen Partner (außer
                # Steuerzeilen) – so übernimmt auch eine Vorlage die Zuordnung.
                line_inputs = apply_entry_partner(
                    session=session,
                    company_id=company_id,
                    lines=line_inputs,
                    partner_id=entry_partner_id,
                )
            entry_payload = JournalEntryInput(
                company_id=company_id,
                entry_date=parsed_date,
                description=description,
                status="posted",
                changed_by=changed_by(),
                lines=line_inputs,
                post_to_closing_period=bool(request.form.get("post_to_closing_period")),
                service_date=service_date,
            )
            entry = create_journal_entry(session=session, payload=entry_payload)

            template_name = (request.form.get("template_name") or "").strip()
            if template_name:
                try:
                    create_template(
                        session=session,
                        company_id=company_id,
                        name=template_name,
                        description=description,
                        interval=request.form.get("template_interval") or "on_demand",
                        lines=[
                            {
                                "account_id": line.account_id,
                                "debit": str(line.debit_amount),
                                "credit": str(line.credit_amount),
                                "tax_code_id": line.tax_code_id,
                                "cost_center_id": line.cost_center_id,
                                "profit_center_id": line.profit_center_id,
                                "partner_id": line.partner_id,
                                "description": line.description,
                            }
                            for line in line_inputs
                        ],
                        changed_by=changed_by(),
                    )
                    flash(f"Vorlage „{template_name}“ wurde gespeichert.", "success")
                except JournalTemplateError as exc:
                    flash(f"Buchung gespeichert, Vorlage aber nicht: {exc}", "warning")

    except (JournalEntryCreationError, JournalEntryValidationError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.journal_page", company_id=company_id, neu=1))
    except IntegrityError:
        flash("Die Buchung konnte nicht gespeichert werden. Bitte erneut versuchen.", "error")
        return redirect(url_for("main.journal_page", company_id=company_id, neu=1))

    flash(f"Buchung {entry.posting_number} wurde gespeichert.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


def _load_entry(session, journal_entry_id: int) -> JournalEntry:
    entry = session.get(JournalEntry, journal_entry_id)
    if entry is None:
        abort(404)
    require_company_access(session, entry.company_id)
    return entry


def _render_entry_detail(journal_entry_id: int):
    """Detailseite einer Buchung: Zeilen, Partner, Belege, Status und Aktionen."""
    session_factory = get_session_factory()
    with session_factory() as session:
        entry = _load_entry(session, journal_entry_id)
        companies, _ = company_context(session)
        overview = entry_overview(session, entry.company_id, [entry.id])[entry.id]
        reversed_by_entry, posting_number_by_id = _reversal_links(session, [entry])
        reversed_by = reversed_by_entry.get(entry.id)
        fiscal_year = session.get(FiscalYear, entry.fiscal_year_id)
        partners = (
            session.execute(
                scoped_select(BusinessPartner, company_id=entry.company_id).order_by(
                    BusinessPartner.name
                )
            )
            .scalars()
            .all()
        )
        tax_accounts = tax_account_ids(session, entry.company_id)
        lines = []
        for line in overview["lines"]:
            # Steuerzeilen bekommen keine Partnerauswahl, solange sie keinen tragen.
            editable = line["account_id"] not in tax_accounts or line["partner_id"] is not None
            choices = []
            for partner in partners:
                role_ok = (
                    line["subledger"] is None
                    or (line["subledger"] == "debtor" and partner.debtor_number)
                    or (line["subledger"] == "creditor" and partner.creditor_number)
                )
                if role_ok and (partner.is_active or partner.id == line["partner_id"]):
                    number = partner_display_number(partner, line["subledger"])
                    choices.append(
                        {
                            "id": partner.id,
                            "label": f"{partner.name} ({number})" if number else partner.name,
                            "is_active": partner.is_active,
                        }
                    )
            lines.append({**line, "partner_editable": editable, "partner_choices": choices})
        history = [
            serialize_audit_log_entry(event)
            for event in session.execute(
                select(AuditLog)
                .where(
                    AuditLog.entity_type == "journal_entry",
                    AuditLog.entity_id == str(entry.id),
                )
                .order_by(AuditLog.changed_at, AuditLog.id)
            ).scalars()
        ]
        context = {
            "entry": entry,
            "overview": overview,
            "lines": lines,
            "documents": documents_by_journal_entry(session, [entry.id]).get(entry.id, []),
            "reversed_by": reversed_by,
            "reversal_of_number": posting_number_by_id.get(entry.reversal_of_id),
            "fiscal_year": fiscal_year,
            "period_number": entry.period.period_number if entry.period else None,
            "amendable": _is_amendable(entry, reversed_by),
            "is_system_entry": entry.source in CARRYFORWARD_SOURCES,
            "can_reverse": not entry.reversal_of_id and not reversed_by,
            "history": history,
            "source_label": SOURCE_LABELS.get(entry.source),
            "all_partners": [partner for partner in partners if partner.is_active],
            "partner_names": {partner.id: partner.name for partner in partners},
        }
        return render_template(
            "buchung_detail.html",
            companies=companies,
            selected_company_id=entry.company_id,
            today=date.today().isoformat(),
            **context,
        )


@main_bp.get("/buchungen/<int:journal_entry_id>")
def journal_entry_detail_page(journal_entry_id: int):
    return _render_entry_detail(journal_entry_id)


@main_bp.get("/buchungen/<int:journal_entry_id>/ergaenzen")
def amend_journal_entry_page(journal_entry_id: int):
    """Offene Buchung ergänzen – Formular auf der Detailseite."""
    return _render_entry_detail(journal_entry_id)


@main_bp.post("/buchungen/<int:journal_entry_id>/ergaenzen")
def amend_journal_entry_action(journal_entry_id: int):
    detail_url = url_for("main.journal_entry_detail_page", journal_entry_id=journal_entry_id)
    options: dict[str, object] = {}
    if "service_date" in request.form:
        service_date_raw = request.form.get("service_date", "").strip()
        try:
            options["service_date"] = (
                date.fromisoformat(service_date_raw) if service_date_raw else None
            )
        except ValueError:
            flash("Ungültiges Leistungsdatum.", "error")
            return redirect(_safe_next(detail_url))
    # Partner der Buchung (Schnellzuordnung im Journal): alle Zeilen außer Steuerzeilen.
    entry_partner = KEEP_PARTNER
    if "partner_id" in request.form:
        raw_partner = request.form.get("partner_id", "").strip()
        entry_partner = int(raw_partner) if raw_partner.isdigit() else None
    options["partner_id"] = entry_partner

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = _load_entry(session, journal_entry_id)
        company_id = entry.company_id
        line_partners: dict[int, int | None] = {}
        for line in entry.lines:
            field = f"line_partner_{line.line_number}"
            if field in request.form:
                raw = request.form.get(field, "").strip()
                line_partners[line.line_number] = int(raw) if raw.isdigit() else None
        before = (
            entry.service_date,
            {line.line_number: line.partner_id for line in entry.lines},
        )
        try:
            entry = amend_journal_entry(
                session=session,
                company_id=company_id,
                journal_entry_id=journal_entry_id,
                changed_by=changed_by(),
                line_partners=line_partners,
                **options,
            )
        except JournalEntryCreationError as exc:
            flash(str(exc), "error")
            return redirect(_safe_next(detail_url))
        posting_number = entry.posting_number
        changed = before != (
            entry.service_date,
            {line.line_number: line.partner_id for line in entry.lines},
        )

    if changed:
        flash(f"Buchung {posting_number} wurde ergänzt.", "success")
    else:
        flash(f"Buchung {posting_number}: keine Änderungen.", "info")
    return redirect(
        _safe_next(url_for("main.journal_entry_detail_page", journal_entry_id=journal_entry_id))
    )


@main_bp.post("/journal-entries/<int:journal_entry_id>/festschreiben")
def finalize_journal_entry_action(journal_entry_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        entry = _load_entry(session, journal_entry_id)
        company_id = entry.company_id
        try:
            entry = finalize_journal_entry(
                session=session, journal_entry_id=journal_entry_id, changed_by=changed_by()
            )
        except JournalEntryCreationError as exc:
            flash(str(exc), "error")
            return redirect(_safe_next(url_for("main.journal_page", company_id=company_id)))

    flash(f"Buchung {entry.posting_number} wurde festgeschrieben.", "success")
    return redirect(_safe_next(url_for("main.journal_page", company_id=company_id)))


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
            f"{count} Buchung(en) bis {up_to_date.strftime('%d.%m.%Y')} wurden festgeschrieben.",
            "success",
        )
    else:
        flash("Keine offenen Buchungen bis zu diesem Datum gefunden.", "success")
    return redirect(url_for("main.journal_page", company_id=company_id))


@main_bp.post("/journal-entries/<int:journal_entry_id>/stornieren")
def reverse_journal_entry_action(journal_entry_id: int):
    reversal_date_raw = request.form.get("reversal_date", "").strip()

    session_factory = get_session_factory()
    with session_factory() as session:
        entry = _load_entry(session, journal_entry_id)
        company_id = entry.company_id
        fallback = _safe_next(url_for("main.journal_page", company_id=company_id))
        try:
            reversal_date = (
                date.fromisoformat(reversal_date_raw) if reversal_date_raw else date.today()
            )
        except ValueError:
            flash("Ungültiges Stornodatum.", "error")
            return redirect(fallback)
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
            return redirect(fallback)

    flash(
        f"Buchung {original_number} wurde storniert (Stornobuchung {reversal.posting_number}).",
        "success",
    )
    return redirect(fallback)
