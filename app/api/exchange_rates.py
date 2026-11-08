"""Wechselkurse und Stichtagsbewertung über die API (ADR-010)."""

from __future__ import annotations

from datetime import date

from flask import jsonify, request

from app.api.blueprint import api_bp
from app.api.helpers import (
    DateArgError,
    api_can_write,
    api_scoped_company,
    date_arg,
    forbidden,
    get_session_factory,
)
from app.auth import current_api_user
from app.services.exchange_rates import (
    ExchangeRateError,
    ExchangeRateSourceError,
    fetch_bmf_rates,
    fetch_ecb_rates,
    format_rate_month,
    fx_valuation_report,
    list_exchange_rates,
    list_vat_exchange_rates,
    post_fx_valuation,
    serialize_exchange_rate,
    serialize_fx_valuation,
    serialize_vat_exchange_rate,
    set_exchange_rate,
    set_vat_exchange_rate,
)
from app.services.journal_entries import JournalEntryCreationError
from domain.services.journal_entry_validation import JournalEntryValidationError


def _api_changed_by() -> str:
    return (current_api_user() or {}).get("username", "api")


def _company_id_from(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@api_bp.get("/exchange-rates")
def list_exchange_rates_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    try:
        date_from = date_arg("date_from")
        date_to = date_arg("date_to")
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            rows = list_exchange_rates(
                session=session,
                tenant_id=company.tenant_id,
                currency_code=(request.args.get("currency_code") or "").strip() or None,
                date_from=date_from,
                date_to=date_to,
                limit=request.args.get("limit", default=200, type=int) or 200,
            )
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "company_id": company_id,
                "exchange_rates": [serialize_exchange_rate(row) for row in rows],
            }
        )


@api_bp.post("/exchange-rates")
def set_exchange_rate_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _company_id_from(payload.get("company_id"))
    if company_id is None:
        return jsonify({"error": "company_id is required."}), 400
    try:
        rate_date = date.fromisoformat(str(payload.get("rate_date") or ""))
    except ValueError:
        return jsonify({"error": "rate_date must be an ISO date (YYYY-MM-DD)."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            row = set_exchange_rate(
                session=session,
                company=company,
                currency_code=payload.get("currency_code"),
                rate_date=rate_date,
                rate=payload.get("rate"),
                changed_by=_api_changed_by(),
            )
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_exchange_rate(row)), 201


@api_bp.post("/exchange-rates/fetch")
def fetch_exchange_rates_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _company_id_from(payload.get("company_id"))
    if company_id is None:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = fetch_ecb_rates(
                session=session, company=company, changed_by=_api_changed_by()
            )
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify({**result, "latest_rate_date": result["latest_rate_date"].isoformat()})


@api_bp.get("/vat-exchange-rates")
def list_vat_exchange_rates_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            rows = list_vat_exchange_rates(
                session=session,
                tenant_id=company.tenant_id,
                currency_code=(request.args.get("currency_code") or "").strip() or None,
                month_from=(request.args.get("month_from") or "").strip() or None,
                month_to=(request.args.get("month_to") or "").strip() or None,
                limit=request.args.get("limit", default=200, type=int) or 200,
            )
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "company_id": company_id,
                "vat_exchange_rates": [serialize_vat_exchange_rate(row) for row in rows],
            }
        )


@api_bp.post("/vat-exchange-rates")
def set_vat_exchange_rate_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _company_id_from(payload.get("company_id"))
    if company_id is None:
        return jsonify({"error": "company_id is required."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            row = set_vat_exchange_rate(
                session=session,
                company=company,
                currency_code=payload.get("currency_code"),
                month=payload.get("month"),
                rate=payload.get("rate"),
                changed_by=_api_changed_by(),
            )
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(serialize_vat_exchange_rate(row)), 201


@api_bp.post("/vat-exchange-rates/fetch")
def fetch_vat_exchange_rates_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _company_id_from(payload.get("company_id"))
    if company_id is None:
        return jsonify({"error": "company_id is required."}), 400
    year = None
    if payload.get("year") not in (None, ""):
        try:
            year = int(payload["year"])
        except (TypeError, ValueError):
            return jsonify({"error": "year must be an integer."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = fetch_bmf_rates(
                session=session, company=company, changed_by=_api_changed_by(), year=year
            )
        except ExchangeRateSourceError as exc:
            return jsonify({"error": str(exc)}), 502
        except ExchangeRateError as exc:
            return jsonify({"error": str(exc)}), 422
        latest = result["latest_month"]
        return jsonify({**result, "latest_month": format_rate_month(latest) if latest else None})


@api_bp.get("/fx-valuation")
def get_fx_valuation_via_api():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return jsonify({"error": "company_id is required."}), 400
    try:
        as_of = date_arg("as_of") or date.today()
    except DateArgError as exc:
        return jsonify({"error": str(exc)}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        return jsonify(
            serialize_fx_valuation(
                fx_valuation_report(session=session, company=company, as_of=as_of)
            )
        )


@api_bp.post("/fx-valuation")
def post_fx_valuation_via_api():
    if not api_can_write():
        return forbidden()
    payload = request.get_json(silent=True) or {}
    company_id = _company_id_from(payload.get("company_id"))
    if company_id is None:
        return jsonify({"error": "company_id is required."}), 400
    try:
        as_of = date.fromisoformat(str(payload.get("as_of") or ""))
    except ValueError:
        return jsonify({"error": "as_of must be an ISO date (YYYY-MM-DD)."}), 400
    session_factory = get_session_factory()
    with session_factory() as session:
        company = api_scoped_company(session, company_id)
        if company is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            result = post_fx_valuation(
                session=session, company=company, as_of=as_of, changed_by=_api_changed_by()
            )
        except (
            ExchangeRateError,
            JournalEntryCreationError,
            JournalEntryValidationError,
        ) as exc:
            session.rollback()
            return jsonify({"error": str(exc)}), 422
        return jsonify(
            {**result, "gain": str(result["gain"]), "loss": str(result["loss"])}
        ), 201
