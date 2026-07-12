"""Kontenrahmen-Import über die API."""

from __future__ import annotations

import base64
import binascii
from io import StringIO
from pathlib import Path

from flask import jsonify, request
from werkzeug.utils import secure_filename

from app.api.blueprint import api_bp
from app.api.helpers import (
    api_can_write,
    api_scoped_company,
    forbidden,
    get_session_factory,
)
from app.services.account_chart_import import (
    BUNDLED_ACCOUNT_CHART_FILES,
    AccountChartImportReport,
    import_account_chart_csv,
    import_bundled_account_chart,
)

ALLOWED_ACCOUNT_CHART_MIME_TYPES = {
    "text/csv",
    "application/vnd.ms-excel",
    "application/octet-stream",
}


def _report_dict(report: AccountChartImportReport) -> dict[str, object]:
    return {
        "total_rows": report.total_rows,
        "imported_rows": report.imported_rows,
        "duplicate_rows": report.duplicate_rows,
        "error_rows": report.error_rows,
        "errors": [
            {"line_number": error.line_number, "message": error.message}
            for error in report.errors
        ],
    }


def _load_import_payload():
    if request.files:
        uploaded_file = request.files.get("account_chart_csv")
        return (
            {
                "company_id": request.form.get("company_id", type=int),
                "chart": (request.form.get("chart") or "").strip().lower() or None,
                "file_name": secure_filename(uploaded_file.filename)
                if uploaded_file and uploaded_file.filename
                else "",
                "mime_type": uploaded_file.mimetype if uploaded_file else "text/csv",
                "content": uploaded_file.read()
                if uploaded_file and uploaded_file.filename
                else None,
            },
            None,
            None,
        )

    payload = request.get_json(silent=True) or {}
    content_base64 = (payload.get("content_base64") or "").strip()
    content = None
    if content_base64:
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError):
            return None, jsonify({"error": "content_base64 must be valid base64."}), 400
    return (
        {
            "company_id": payload.get("company_id"),
            "chart": (payload.get("chart") or "").strip().lower() or None,
            "file_name": secure_filename((payload.get("file_name") or "").strip()),
            "mime_type": payload.get("mime_type") or "text/csv",
            "content": content,
        },
        None,
        None,
    )


def _validate_csv_upload(*, file_name: str, mime_type: str) -> str | None:
    if not file_name:
        return "file_name is required when content_base64 is used."
    if Path(file_name).suffix.lower() != ".csv":
        return "Only CSV account chart files may be imported."
    if mime_type not in ALLOWED_ACCOUNT_CHART_MIME_TYPES:
        return "Account chart CSV MIME type is not allowed."
    return None


@api_bp.post("/account-chart/import")
def import_account_chart_via_api():
    if not api_can_write():
        return forbidden()

    payload, error_response, status_code = _load_import_payload()
    if error_response is not None:
        return error_response, status_code

    try:
        company_id = int(payload["company_id"])
    except (TypeError, ValueError):
        return jsonify({"error": "company_id is required."}), 400

    chart = payload["chart"]
    content = payload["content"]
    if bool(chart) == bool(content):
        return (
            jsonify({"error": "Provide exactly one of chart or content_base64/file upload."}),
            400,
        )
    if chart is not None and chart not in BUNDLED_ACCOUNT_CHART_FILES:
        return jsonify({"error": "chart must be 'skr03' or 'skr04'."}), 400
    if content is not None:
        validation_error = _validate_csv_upload(
            file_name=payload["file_name"], mime_type=payload["mime_type"]
        )
        if validation_error:
            return jsonify({"error": validation_error}), 422

    session_factory = get_session_factory()
    with session_factory() as session:
        if api_scoped_company(session, company_id) is None:
            return jsonify({"error": "Company not found."}), 404
        try:
            if chart is not None:
                report = import_bundled_account_chart(
                    session=session, company_id=company_id, chart=chart
                )
            else:
                csv_text = content.decode("utf-8-sig")
                report = import_account_chart_csv(
                    session=session,
                    company_id=company_id,
                    csv_stream=StringIO(csv_text),
                )
        except UnicodeDecodeError:
            return jsonify({"error": "CSV must be UTF-8 encoded."}), 422
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422

    return (
        jsonify(
            {
                "company_id": company_id,
                "chart": chart,
                "report": _report_dict(report),
            }
        ),
        201,
    )
