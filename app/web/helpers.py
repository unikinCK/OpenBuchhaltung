"""Gemeinsame Helfer für alle UI-Routenmodule des main-Blueprints."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from flask import abort, current_app, request

from app.auth import current_tenant_id, current_user
from app.services.documents import (
    DOCUMENT_SIGNATURE_PROBE_BYTES,
    document_content_error_code,
)
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


DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def pagination_args(
    default: int = DEFAULT_PAGE_SIZE, maximum: int = MAX_PAGE_SIZE
) -> tuple[int, int]:
    """Liest limit/offset aus der Query, geclampt auf sinnvolle Grenzen."""
    limit = request.args.get("limit", type=int)
    limit = default if limit is None else max(1, min(limit, maximum))
    offset = max(0, request.args.get("offset", type=int) or 0)
    return limit, offset


def search_args() -> tuple[str | None, date | None, date | None]:
    """Suchbegriff und Datumsbereich aus der Query (ungültige Daten -> None)."""
    query = (request.args.get("q") or "").strip() or None

    def parse(name: str) -> date | None:
        raw = (request.args.get(name) or "").strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    return query, parse("date_from"), parse("date_to")


def filter_url_args(
    query: str | None, date_from, date_to, **extra: object
) -> dict[str, object]:
    """Nicht-leere Filterwerte als url_for-Parameter (für Paginierungslinks)."""
    args: dict[str, object] = {}
    if query:
        args["q"] = query
    if date_from:
        args["date_from"] = date_from.isoformat()
    if date_to:
        args["date_to"] = date_to.isoformat()
    args.update({key: value for key, value in extra.items() if value})
    return args


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


def _uploaded_file_head(uploaded_file, num_bytes: int) -> bytes | None:
    stream = uploaded_file.stream
    if not hasattr(stream, "tell") or not hasattr(stream, "seek"):
        return None
    current_position = stream.tell()
    stream.seek(0)
    head = stream.read(num_bytes)
    stream.seek(current_position)
    return head


DOCUMENT_UPLOAD_ERROR_MESSAGES = {
    "too_large": "Der Beleg ist zu groß.",
    "too_small": (
        "Der Beleg ist zu klein und wirkt unvollständig – bitte die Originaldatei "
        "ohne zusätzliche Komprimierung hochladen."
    ),
    "signature_mismatch": (
        "Der Dateiinhalt passt nicht zum Dateityp – der Beleg ist vermutlich "
        "beschädigt oder wurde beim Verkleinern zerstört."
    ),
}


def document_upload_error(uploaded_file, file_name: str) -> str | None:
    extension = Path(file_name).suffix.lower()
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        return "Nur PDF-, JPG- und PNG-Belege dürfen hochgeladen werden."

    mimetype = uploaded_file.mimetype or "application/octet-stream"
    if mimetype not in ALLOWED_DOCUMENT_MIME_TYPES:
        return "Der Dateityp des Belegs ist nicht erlaubt."

    file_size = _uploaded_file_size(uploaded_file)
    if file_size is None:
        return None
    head = _uploaded_file_head(uploaded_file, DOCUMENT_SIGNATURE_PROBE_BYTES)
    error_code = document_content_error_code(
        mime_type=mimetype,
        content_head=head or b"",
        content_size=file_size,
        max_bytes=current_app.config.get("DOCUMENT_MAX_UPLOAD_BYTES"),
        min_bytes=current_app.config.get("DOCUMENT_MIN_UPLOAD_BYTES"),
    )
    if error_code:
        return DOCUMENT_UPLOAD_ERROR_MESSAGES[error_code]

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
