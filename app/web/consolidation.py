"""Konsolidierung: Gruppen pflegen, Bericht mit Spalten je Gesellschaft, manuelle Buchungen."""

from __future__ import annotations

from datetime import date
from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for

from app.auth import WRITE_ROLES, current_tenant_id, current_user
from app.services.consolidation import (
    KIND_LABELS,
    ConsolidationError,
    account_choices,
    consolidation_report,
    create_adjustment,
    create_group,
    delete_adjustment,
    list_groups,
    remove_member,
    set_member,
    sorted_members,
    update_group,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    filter_url_args,
    get_session_factory,
    require_company_access,
    search_args,
)
from domain.models import ConsolidationAdjustment, ConsolidationGroup

# So viele Zeilen bietet das Formular für eine manuelle Buchung an.
ADJUSTMENT_FORM_LINES = 6


def _can_write() -> bool:
    user = current_user()
    return bool(user and user["role"] in WRITE_ROLES)


def _load_group(session, group_id: int) -> ConsolidationGroup:
    group = session.get(ConsolidationGroup, group_id)
    if group is None:
        abort(404)
    tenant_id = current_tenant_id()
    if tenant_id is not None and group.tenant_id != tenant_id:
        abort(404)
    return group


def _closing_flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "on", "yes", "ja"}


def _selected_group(
    groups: list[ConsolidationGroup], company_id: int | None
) -> ConsolidationGroup | None:
    """Gewählte Gruppe, sonst die erste mit der ausgewählten Gesellschaft, sonst die erste."""
    requested = request.args.get("group_id", type=int)
    by_id = {group.id: group for group in groups}
    if requested in by_id:
        return by_id[requested]
    for group in groups:
        if any(member.company_id == company_id for member in group.members):
            return group
    return groups[0] if groups else None


@main_bp.get("/konsolidierung")
def consolidation_page():
    _, date_from, date_to = search_args()
    include_closing_entries = _closing_flag(request.args.get("include_closing_entries"))
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        groups = list_groups(session=session, tenant_id=current_tenant_id())
        group = _selected_group(groups, selected_company_id)
        report = None
        report_error = None
        accounts: list[dict[str, str]] = []
        if group is not None:
            accounts = account_choices(session=session, group=group)
            try:
                report = consolidation_report(
                    session=session,
                    group=group,
                    date_from=date_from,
                    date_to=date_to,
                    include_closing_entries=include_closing_entries,
                )
            except ConsolidationError as exc:
                report_error = str(exc)
        member_ids = {member.company_id for member in group.members} if group else set()
        return render_template(
            "konsolidierung.html",
            companies=companies,
            selected_company_id=selected_company_id,
            groups=groups,
            group=group,
            members=sorted_members(group) if group else [],
            report=report,
            report_error=report_error,
            accounts=accounts,
            addable_companies=[
                company
                for company in companies
                if group is not None
                and company.id not in member_ids
                and company.tenant_id == group.tenant_id
            ],
            parent_choices=[
                company
                for company in companies
                if group is not None and company.tenant_id == group.tenant_id
            ],
            kind_labels=KIND_LABELS,
            adjustment_form_lines=range(ADJUSTMENT_FORM_LINES),
            today=date.today(),
            can_write=_can_write(),
            date_from=date_from,
            date_to=date_to,
            include_closing_entries=include_closing_entries,
            report_args=filter_url_args(
                None,
                date_from,
                date_to,
                include_closing_entries="1" if include_closing_entries else None,
            ),
        )


def _back_to_group(group_id: int | None):
    """Zurück zur Seite; der Auswertungszeitraum aus dem Formular bleibt erhalten."""
    args: dict[str, Any] = {
        key: value
        for key in ("date_from", "date_to", "include_closing_entries")
        if (value := (request.form.get(key) or "").strip())
    }
    if group_id is not None:
        args["group_id"] = group_id
    return redirect(url_for("main.consolidation_page", **args))


@main_bp.post("/konsolidierung")
def create_consolidation_group_from_form():
    parent_company_id = request.form.get("parent_company_id", type=int)
    if not parent_company_id:
        flash("Bitte eine Muttergesellschaft wählen.", "error")
        return _back_to_group(None)
    session_factory = get_session_factory()
    with session_factory() as session:
        parent_company = require_company_access(session, parent_company_id)
        try:
            group = create_group(
                session=session,
                parent_company=parent_company,
                changed_by=changed_by(),
                name=request.form.get("name", ""),
            )
        except ConsolidationError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _back_to_group(None)
        session.commit()
        group_id = group.id
        group_name = group.name
    flash(f"Gruppe „{group_name}“ wurde angelegt.", "success")
    return _back_to_group(group_id)


@main_bp.post("/konsolidierung/<int:group_id>")
def update_consolidation_group_from_form(group_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _load_group(session, group_id)
        try:
            changed = update_group(
                session=session,
                group=group,
                changed_by=changed_by(),
                name=request.form.get("name", ""),
                parent_company_id=request.form.get("parent_company_id", type=int),
                is_active=request.form.get("is_active") == "1",
            )
        except ConsolidationError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _back_to_group(group_id)
        session.commit()
    flash("Gruppe wurde geändert." if changed else "Keine Änderung erkannt.", "success")
    return _back_to_group(group_id)


@main_bp.post("/konsolidierung/<int:group_id>/mitglieder")
def set_consolidation_member_from_form(group_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _load_group(session, group_id)
        try:
            changed = set_member(
                session=session,
                group=group,
                changed_by=changed_by(),
                company_id=request.form.get("company_id"),
                ownership_percent=request.form.get("ownership_percent"),
                included_from=request.form.get("included_from"),
                included_to=request.form.get("included_to"),
            )
        except ConsolidationError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _back_to_group(group_id)
        session.commit()
    flash("Mitglied wurde gespeichert." if changed else "Keine Änderung erkannt.", "success")
    return _back_to_group(group_id)


@main_bp.post("/konsolidierung/<int:group_id>/mitglieder/<int:company_id>/entfernen")
def remove_consolidation_member_from_form(group_id: int, company_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _load_group(session, group_id)
        try:
            remove_member(
                session=session, group=group, changed_by=changed_by(), company_id=company_id
            )
        except ConsolidationError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _back_to_group(group_id)
        session.commit()
    flash("Mitglied wurde entfernt.", "success")
    return _back_to_group(group_id)


def _adjustment_lines_from_form() -> list[dict[str, str]]:
    """Formularzeilen ohne Kontonummer und ohne Betrag zählen nicht."""
    codes = request.form.getlist("line_account_code")
    debits = request.form.getlist("line_debit_amount")
    credits = request.form.getlist("line_credit_amount")
    lines = []
    for code, debit, credit in zip(codes, debits, credits):
        if code.strip() or debit.strip() or credit.strip():
            lines.append({"account_code": code, "debit_amount": debit, "credit_amount": credit})
    return lines


@main_bp.post("/konsolidierung/<int:group_id>/buchungen")
def create_consolidation_adjustment_from_form(group_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _load_group(session, group_id)
        try:
            create_adjustment(
                session=session,
                group=group,
                changed_by=changed_by(),
                adjustment_date=request.form.get("adjustment_date"),
                kind=request.form.get("kind"),
                description=request.form.get("description", ""),
                lines=_adjustment_lines_from_form(),
            )
        except ConsolidationError as exc:
            session.rollback()
            flash(str(exc), "error")
            return _back_to_group(group_id)
        session.commit()
    flash("Konsolidierungsbuchung wurde erfasst.", "success")
    return _back_to_group(group_id)


@main_bp.post("/konsolidierung/<int:group_id>/buchungen/<int:adjustment_id>/loeschen")
def delete_consolidation_adjustment_from_form(group_id: int, adjustment_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        group = _load_group(session, group_id)
        adjustment = session.get(ConsolidationAdjustment, adjustment_id)
        if adjustment is None or adjustment.group_id != group.id:
            abort(404)
        delete_adjustment(
            session=session,
            adjustment=adjustment,
            changed_by=changed_by(),
            reason=request.form.get("reason"),
        )
        session.commit()
    flash("Konsolidierungsbuchung wurde gelöscht; ihr Inhalt steht im Audit-Log.", "success")
    return _back_to_group(group_id)
