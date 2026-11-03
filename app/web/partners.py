"""Geschäftspartner (Kunden/Debitoren, Lieferanten/Kreditoren): Liste, Anlage, Pflege."""

from __future__ import annotations

from datetime import date
from typing import Any

from flask import abort, flash, redirect, render_template, request, url_for
from sqlalchemy import case, func, select

from app.services.partner_ledger import (
    SIDE_LABELS,
    SUBLEDGER_SIDES,
    partner_balances,
    partner_statement,
)
from app.services.partner_takeover import (
    PartnerTakeoverError,
    apply_partner_takeover,
    partner_takeover_proposals,
)
from app.services.partners import (
    PartnerError,
    create_partner,
    duplicate_hints,
    list_partners,
    partner_history,
    set_partner_bank_details,
    update_partner,
)
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    pagination_args,
    require_company_access,
    search_args,
    sort_args,
)
from app.web.journal import entry_overview
from domain.models import (
    SUBLEDGER_DEBTOR,
    SUBLEDGER_TYPES,
    BusinessPartner,
    Company,
    JournalEntry,
    JournalEntryLine,
)

# Buchungen eines Partners auf der Detailseite (vollständige Liste im Journal).
PARTNER_ENTRY_LIMIT = 25


def _entry_counts(session, partner_ids: list[int]) -> dict[int, int]:
    """Anzahl Buchungen je Partner (Buchungen mit mindestens einer Zeile des Partners)."""
    if not partner_ids:
        return {}
    return dict(
        session.execute(
            select(
                JournalEntryLine.partner_id,
                func.count(func.distinct(JournalEntryLine.journal_entry_id)),
            )
            .where(JournalEntryLine.partner_id.in_(partner_ids))
            .group_by(JournalEntryLine.partner_id)
        ).all()
    )

_TEXT_FIELDS = (
    "name",
    "partner_kind",
    "street",
    "postal_code",
    "city",
    "country_code",
    "vat_id",
    "tax_number",
    "email",
    "phone",
    "contact_person",
    "payment_term_days",
    "notes",
    "debtor_number",
    "creditor_number",
    "intercompany_company_id",
)


def _intercompany_choices(session, company_id: int) -> list[dict[str, Any]]:
    """Andere Gesellschaften des Mandanten als Auswahl „Verbundene Gesellschaft“."""
    company = session.get(Company, company_id)
    rows = session.execute(
        select(Company.id, Company.name)
        .where(Company.tenant_id == company.tenant_id, Company.id != company_id)
        .order_by(Company.name)
    ).all()
    return [{"id": row.id, "name": row.name} for row in rows]


def _form_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {
        field: request.form.get(field, "") for field in _TEXT_FIELDS if field in request.form
    }
    fields["is_customer"] = request.form.get("is_customer") == "1"
    fields["is_supplier"] = request.form.get("is_supplier") == "1"
    # Ohne Rolle zählt eine (vorbelegte) Nummer nicht – die Rolle wird entfernt.
    if not fields["is_customer"]:
        fields.pop("debtor_number", None)
    if not fields["is_supplier"]:
        fields.pop("creditor_number", None)
    return fields


def _flash_hints(hints: list[str]) -> None:
    for hint in hints:
        flash(f"Hinweis: {hint}", "warning")


PARTNER_SORT_COLUMNS = (
    "name",
    "role",
    "debtor",
    "creditor",
    "city",
    "vat_id",
    "payment_terms",
    "entries",
)


def _blank_as_null(column):
    return func.nullif(func.trim(column), "")


def _partner_sort_expressions(key: str) -> tuple:
    if key == "role":
        # Wie angezeigt: Kunde, Kunde + Lieferant, Lieferant, ohne Rolle.
        has_debtor = BusinessPartner.debtor_number.is_not(None)
        has_creditor = BusinessPartner.creditor_number.is_not(None)
        return (
            case(
                (has_debtor & has_creditor, 2),
                (has_debtor, 1),
                (has_creditor, 3),
                else_=None,
            ),
        )
    if key == "debtor":
        return (_blank_as_null(BusinessPartner.debtor_number),)
    if key == "creditor":
        return (_blank_as_null(BusinessPartner.creditor_number),)
    if key == "city":
        return (
            func.lower(_blank_as_null(BusinessPartner.city)),
            BusinessPartner.postal_code,
        )
    if key == "vat_id":
        return (_blank_as_null(BusinessPartner.vat_id),)
    if key == "payment_terms":
        return (BusinessPartner.payment_term_days,)
    if key == "entries":
        return (
            select(func.count(func.distinct(JournalEntryLine.journal_entry_id)))
            .where(JournalEntryLine.partner_id == BusinessPartner.id)
            .scalar_subquery(),
        )
    return (func.lower(BusinessPartner.name),)


@main_bp.get("/partner")
def partners_page():
    limit, offset = pagination_args()
    sort = sort_args(PARTNER_SORT_COLUMNS, "name")
    role = (request.args.get("role") or "").strip() or None
    if role not in SUBLEDGER_TYPES:
        role = None
    query = (request.args.get("q") or "").strip() or None
    include_inactive = request.args.get("include_inactive") == "1"
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        partners: list[BusinessPartner] = []
        total = 0
        entry_counts: dict[int, int] = {}
        intercompany_choices: list[dict[str, Any]] = []
        if selected_company_id:
            intercompany_choices = _intercompany_choices(session, selected_company_id)
            partners, total = list_partners(
                session=session,
                company_id=selected_company_id,
                role=role,
                query=query,
                include_inactive=include_inactive,
                limit=limit,
                offset=offset,
                order_by=sort.order_by(
                    *_partner_sort_expressions(sort.key),
                    then=(func.lower(BusinessPartner.name), BusinessPartner.id),
                ),
            )
            entry_counts = _entry_counts(session, [partner.id for partner in partners])
    filter_args = {
        key: value
        for key, value in (
            ("role", role),
            ("q", query),
            ("include_inactive", "1" if include_inactive else None),
        )
        if value
    }
    return render_template(
        "partner.html",
        companies=companies,
        selected_company_id=selected_company_id,
        partners=partners,
        total=total,
        entry_counts=entry_counts,
        limit=limit,
        offset=offset,
        role=role,
        q=query,
        include_inactive=include_inactive,
        filter_args=filter_args,
        sort=sort,
        intercompany_choices=intercompany_choices,
    )


@main_bp.post("/partner")
def create_partner_from_form():
    company_id = request.form.get("company_id", type=int)
    if not company_id:
        abort(404)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            partner = create_partner(
                session=session,
                company=company,
                changed_by=changed_by(),
                **_form_fields(),
            )
            iban = request.form.get("iban", "").strip()
            bic = request.form.get("bic", "").strip()
            if iban or bic:
                set_partner_bank_details(
                    session=session,
                    partner=partner,
                    changed_by=changed_by(),
                    iban=iban,
                    bic=bic,
                )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partners_page", company_id=company_id))
        hints = duplicate_hints(
            session=session,
            company_id=company.id,
            name=partner.name,
            vat_id=partner.vat_id,
            iban=partner.iban,
            exclude_id=partner.id,
        )
        session.commit()
        partner_id = partner.id
        partner_name = partner.name
    flash(f"Geschäftspartner „{partner_name}“ wurde angelegt.", "success")
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))


def _load_partner(session, partner_id: int) -> BusinessPartner:
    partner = session.get(BusinessPartner, partner_id)
    if partner is None:
        abort(404)
    require_company_access(session, partner.company_id)
    return partner


@main_bp.get("/partner/<int:partner_id>")
def partner_detail_page(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        companies, _ = company_context(session)
        history = partner_history(session=session, partner_id=partner.id, limit=100)
        entry_ids = select(JournalEntryLine.journal_entry_id).where(
            JournalEntryLine.partner_id == partner.id
        )
        entries_total = session.execute(
            select(func.count()).select_from(
                select(JournalEntry.id).where(JournalEntry.id.in_(entry_ids)).subquery()
            )
        ).scalar_one()
        entries = (
            session.execute(
                select(JournalEntry)
                .where(JournalEntry.id.in_(entry_ids))
                .order_by(JournalEntry.entry_date.desc(), JournalEntry.id.desc())
                .limit(PARTNER_ENTRY_LIMIT)
            )
            .scalars()
            .all()
        )
        overview = entry_overview(session, partner.company_id, [entry.id for entry in entries])
        _, date_from, date_to = search_args()
        if date_from and date_to and date_from > date_to:
            flash("Das Von-Datum darf nicht nach dem Bis-Datum liegen.", "error")
            date_from = date_to = None
        statement = partner_statement(
            session=session, partner=partner, date_from=date_from, date_to=date_to
        )
        return render_template(
            "partner_detail.html",
            companies=companies,
            selected_company_id=partner.company_id,
            partner=partner,
            history=history,
            statement=statement,
            date_from=date_from,
            date_to=date_to,
            entries=entries,
            entries_total=entries_total,
            overview=overview,
            intercompany_choices=_intercompany_choices(session, partner.company_id),
        )


@main_bp.get("/partner/uebernahme")
def partner_takeover_page():
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        proposals = (
            partner_takeover_proposals(session=session, company_id=selected_company_id)
            if selected_company_id
            else []
        )
    return render_template(
        "partner_uebernahme.html",
        companies=companies,
        selected_company_id=selected_company_id,
        proposals=proposals,
    )


@main_bp.post("/partner/uebernahme")
def apply_partner_takeover_action():
    company_id = request.form.get("company_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        require_company_access(session, company_id)
        try:
            result = apply_partner_takeover(
                session=session,
                company_id=company_id,
                key=request.form.get("key", ""),
                partner_id=request.form.get("partner_id", type=int),
                changed_by=changed_by(),
            )
        except PartnerTakeoverError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.partner_takeover_page", company_id=company_id))
    flash(
        f"{len(result['linked_open_item_ids'])} Posten sind jetzt {result['partner_name']} "
        "zugeordnet.",
        "success",
    )
    for skipped in result["skipped"]:
        flash(f"{skipped['reference']}: {skipped['reason']}", "warning")
    return redirect(url_for("main.partner_takeover_page", company_id=company_id))


@main_bp.get("/partner/saldenliste")
def partner_balances_page():
    side = request.args.get("side", SUBLEDGER_DEBTOR)
    if side not in SUBLEDGER_SIDES:
        side = SUBLEDGER_DEBTOR
    raw_as_of = (request.args.get("as_of") or "").strip()
    try:
        as_of = date.fromisoformat(raw_as_of) if raw_as_of else date.today()
    except ValueError:
        flash("Ungültiger Stichtag.", "error")
        as_of = date.today()
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        report = None
        if selected_company_id:
            report = partner_balances(
                session=session, company_id=selected_company_id, side=side, as_of=as_of
            )
    return render_template(
        "partner_salden.html",
        companies=companies,
        selected_company_id=selected_company_id,
        report=report,
        side=side,
        side_labels=SIDE_LABELS,
        as_of=as_of,
    )


@main_bp.post("/partner/<int:partner_id>/update")
def update_partner_from_form(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        fields = _form_fields()
        fields["is_active"] = request.form.get("is_active") == "true"
        try:
            changed = update_partner(
                session=session,
                partner=partner,
                changed_by=changed_by(),
                **fields,
            )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
        hints = duplicate_hints(
            session=session,
            company_id=partner.company_id,
            name=partner.name,
            vat_id=partner.vat_id,
            exclude_id=partner.id,
        )
        session.commit()
    flash("Geschäftspartner wurde geändert." if changed else "Keine Änderung erkannt.", "success")
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))


@main_bp.post("/partner/<int:partner_id>/bankdaten")
def set_partner_bank_details_from_form(partner_id: int):
    session_factory = get_session_factory()
    with session_factory() as session:
        partner = _load_partner(session, partner_id)
        try:
            changed = set_partner_bank_details(
                session=session,
                partner=partner,
                changed_by=changed_by(),
                iban=request.form.get("iban", ""),
                bic=request.form.get("bic", ""),
            )
        except PartnerError as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
        hints = (
            duplicate_hints(
                session=session,
                company_id=partner.company_id,
                iban=partner.iban,
                exclude_id=partner.id,
            )
            if partner.iban
            else []
        )
        session.commit()
    flash(
        "Bankverbindung wurde gespeichert." if changed else "Keine Änderung erkannt.",
        "success",
    )
    _flash_hints(hints)
    return redirect(url_for("main.partner_detail_page", partner_id=partner_id))
