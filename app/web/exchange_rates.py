"""Fremdwährung: Wechselkurse pflegen und offene Posten zum Stichtag bewerten (ADR-010)."""

from __future__ import annotations

from datetime import date

from flask import flash, redirect, render_template, request, url_for

from app.services.exchange_rates import (
    ExchangeRateError,
    fetch_bmf_rates,
    fetch_ecb_rates,
    format_rate_month,
    fx_valuation_report,
    list_exchange_rates,
    list_vat_exchange_rates,
    post_fx_valuation,
    set_exchange_rate,
    set_vat_exchange_rate,
)
from app.services.journal_entries import JournalEntryCreationError
from app.web.blueprint import main_bp
from app.web.helpers import (
    changed_by,
    company_context,
    get_session_factory,
    require_company_access,
)
from domain.models import Company
from domain.services.journal_entry_validation import JournalEntryValidationError


def _as_of() -> date:
    raw = (request.args.get("as_of") or "").strip()
    try:
        return date.fromisoformat(raw) if raw else date.today()
    except ValueError:
        flash("Ungültiger Stichtag.", "error")
        return date.today()


@main_bp.get("/fremdwaehrung")
def exchange_rates_page():
    as_of = _as_of()
    session_factory = get_session_factory()
    with session_factory() as session:
        companies, selected_company_id = company_context(session)
        rates = []
        vat_rates = []
        report = None
        company_currency = None
        if selected_company_id:
            company = session.get(Company, selected_company_id)
            company_currency = company.currency_code
            rates = list_exchange_rates(
                session=session, tenant_id=company.tenant_id, limit=60
            )
            vat_rates = list_vat_exchange_rates(
                session=session, tenant_id=company.tenant_id, limit=60
            )
            report = fx_valuation_report(session=session, company=company, as_of=as_of)
        return render_template(
            "fremdwaehrung.html",
            companies=companies,
            selected_company_id=selected_company_id,
            company_currency=company_currency,
            rates=rates,
            vat_rates=vat_rates,
            report=report,
            as_of=as_of,
            today=date.today().isoformat(),
            this_month=date.today().strftime("%Y-%m"),
            this_year=date.today().year,
        )


@main_bp.post("/fremdwaehrung/kurs")
def set_exchange_rate_action():
    company_id = request.form.get("company_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            row = set_exchange_rate(
                session=session,
                company=company,
                currency_code=request.form.get("currency_code"),
                rate_date=date.fromisoformat(request.form.get("rate_date", "")),
                rate=request.form.get("rate"),
                changed_by=changed_by(),
            )
        except ValueError as exc:
            # ExchangeRateError ist ein ValueError, ebenso ein ungültiges Datum.
            flash(str(exc), "error")
            return redirect(url_for("main.exchange_rates_page", company_id=company_id))
        flash(f"Kurs für {row.currency_code} wurde gespeichert.", "success")
    return redirect(url_for("main.exchange_rates_page", company_id=company_id))


@main_bp.post("/fremdwaehrung/ezb")
def fetch_exchange_rates_action():
    company_id = request.form.get("company_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            result = fetch_ecb_rates(session=session, company=company, changed_by=changed_by())
        except ExchangeRateError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.exchange_rates_page", company_id=company_id))
    flash(
        f"{result['created']} neue Kurse übernommen, neuester vom "
        f"{result['latest_rate_date'].strftime('%d.%m.%Y')}.",
        "success",
    )
    return redirect(url_for("main.exchange_rates_page", company_id=company_id))


@main_bp.post("/fremdwaehrung/monatskurs")
def set_vat_exchange_rate_action():
    company_id = request.form.get("company_id", type=int)
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            row = set_vat_exchange_rate(
                session=session,
                company=company,
                currency_code=request.form.get("currency_code"),
                month=request.form.get("month"),
                rate=request.form.get("rate"),
                changed_by=changed_by(),
            )
        except ExchangeRateError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.exchange_rates_page", company_id=company_id))
        flash(
            f"Monatskurs für {row.currency_code} {format_rate_month(row.rate_month)} wurde "
            "gespeichert.",
            "success",
        )
    return redirect(url_for("main.exchange_rates_page", company_id=company_id))


@main_bp.post("/fremdwaehrung/bmf")
def fetch_vat_exchange_rates_action():
    company_id = request.form.get("company_id", type=int)
    raw_year = (request.form.get("year") or "").strip()
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            if raw_year and not raw_year.isdigit():
                raise ExchangeRateError(f"Ungültiges Jahr: {raw_year}.")
            result = fetch_bmf_rates(
                session=session,
                company=company,
                changed_by=changed_by(),
                year=int(raw_year) if raw_year else None,
            )
        except ExchangeRateError as exc:
            flash(str(exc), "error")
            return redirect(url_for("main.exchange_rates_page", company_id=company_id))
    latest = result["latest_month"]
    message = f"{result['created']} neue und {result['updated']} geänderte Monatskurse übernommen"
    flash(f"{message}, neuester für {format_rate_month(latest)}." if latest else f"{message}.",
          "success")
    if result["ignored"]:
        flash(
            f"{result['ignored']} Angaben der BMF-Datei waren nicht lesbar und fehlen. "
            "Bitte bei Bedarf von Hand erfassen.",
            "warning",
        )
    return redirect(url_for("main.exchange_rates_page", company_id=company_id))


@main_bp.post("/fremdwaehrung/bewertung")
def post_fx_valuation_action():
    company_id = request.form.get("company_id", type=int)
    raw_as_of = request.form.get("as_of", "")
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            result = post_fx_valuation(
                session=session,
                company=company,
                as_of=date.fromisoformat(raw_as_of),
                changed_by=changed_by(),
            )
        except (
            ValueError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            session.rollback()
            flash(str(exc), "error")
            return redirect(
                url_for("main.exchange_rates_page", company_id=company_id, as_of=raw_as_of)
            )
    flash(
        f"Bewertung gebucht ({result['posting_number']}), Auflösung "
        f"{result['reversal_posting_number']}.",
        "success",
    )
    return redirect(url_for("main.exchange_rates_page", company_id=company_id, as_of=raw_as_of))
