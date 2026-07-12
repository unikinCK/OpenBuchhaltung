"""Gemeinsame Helfer für alle UI-Routenmodule des main-Blueprints."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from flask import abort, current_app, request

from app.auth import current_tenant_id, current_user
from app.services.journal_entries import parse_decimal
from app.services.scoping import scoped_select
from domain.models import Company, FiscalYear, Period

ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_DOCUMENT_MIME_TYPES = {"application/pdf", "image/jpeg", "image/png"}

MONTH_NAMES = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def get_session_factory():
    session_factory = current_app.extensions.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("DB session factory is not configured")
    return session_factory


def require_company_access(session, company_id: int) -> Company:
    """Load a company and enforce that it belongs to the user's tenant scope."""
    company = session.get(Company, company_id)
    if company is None:
        abort(404)
    tenant_id = current_tenant_id()
    if tenant_id is not None and company.tenant_id != tenant_id:
        abort(404)
    return company


def require_period_access(session, period_id: int) -> Period:
    period = session.get(Period, period_id)
    if period is None:
        abort(404)
    fiscal_year = session.get(FiscalYear, period.fiscal_year_id)
    require_company_access(session, fiscal_year.company_id)
    return period


def changed_by() -> str:
    user = current_user()
    return user["username"] if user else "web-form"


def company_context(session) -> tuple[list[Company], int | None]:
    """Companies im Tenant-Scope plus validierte Auswahl aus ?company_id=."""
    tenant_scope = current_tenant_id()
    companies = (
        session.execute(scoped_select(Company, tenant_id=tenant_scope).order_by(Company.name))
        .scalars()
        .all()
    )

    selected_company_id = request.args.get("company_id", type=int)
    accessible_ids = {company.id for company in companies}
    if selected_company_id is not None and selected_company_id not in accessible_ids:
        selected_company_id = None
    if selected_company_id is None and companies:
        selected_company_id = companies[0].id
    return companies, selected_company_id


def _uploaded_file_size(uploaded_file) -> int | None:
    stream = uploaded_file.stream
    if not hasattr(stream, "tell") or not hasattr(stream, "seek"):
        return None
    current_position = stream.tell()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(current_position)
    return size


def document_upload_error(uploaded_file, file_name: str) -> str | None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        return "Nur PDF-, JPG- und PNG-Belege dürfen hochgeladen werden."

    mimetype = uploaded_file.mimetype or "application/octet-stream"
    if mimetype not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "Der Dateityp des Belegs ist nicht erlaubt."

    max_bytes = current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES")
    file_size = _uploaded_file_size(uploaded_file)
    if max_bytes and file_size is not None and file_size > max_bytes:
        return "Der Beleg ist zu groß."

    return None


def optional_decimal(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    return parse_decimal(raw) if raw else None


def safe_optional_decimal(raw: str) -> Decimal | None:
    """Wie ``optional_decimal``, liefert bei ungültiger Eingabe aber ``None``."""
    try:
        return optional_decimal(raw)
    except ValueError:
        return None
