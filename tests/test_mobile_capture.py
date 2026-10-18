"""Mobile Belegerfassung: Erfassungsweg, Aufnahmezeitpunkt, Hash vom Gerät, Idempotenz."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select

from app.services.audit_export import FIELD_DESCRIPTIONS
from app.services.documents import DocumentCaptureError, parse_document_capture
from app.services.mcp_server import TOOLS
from domain.models import AuditLog, Document, ReceiptMatchSuggestion
from tests.test_receipt_ocr import (
    _create_test_app,
    _logged_in_client,
    _pdf_with_text,
    _seed_company_with_accounts,
)

PDF = _pdf_with_text(
    [
        "Muster Lieferant GmbH",
        "Rechnung Nr. 2026-4711",
        "Rechnungsdatum: 08.07.2026",
        "Nettobetrag 200,00 EUR",
        "MwSt 19 % 38,00 EUR",
        "Gesamtbetrag 238,00 EUR",
    ]
)
PDF_SHA256 = hashlib.sha256(PDF).hexdigest()
CAPTURED_AT = "2026-10-13T18:15:00+02:00"


def _setup(tmp_path):
    app = _create_test_app(tmp_path, DOCUMENT_UPLOAD_DIR=str(tmp_path / "uploads"))
    company_id = _seed_company_with_accounts(app)
    return app, company_id, _logged_in_client(app)


def _upload(client, company_id: int, content: bytes = PDF, **fields):
    payload = {
        "company_id": company_id,
        "file_name": "scan.pdf",
        "mime_type": "application/pdf",
        "content_base64": base64.b64encode(content).decode("ascii"),
        "document_date": "2026-10-13",
        "capture_source": "mobile_scan",
        "captured_at": CAPTURED_AT,
        "capture_device": "iPhone 17 Pro",
        "client_sha256": PDF_SHA256,
    }
    payload.update(fields)
    return client.post("/api/v1/documents", json=payload)


def _documents(app) -> list[Document]:
    with app.extensions["db_session_factory"]() as session:
        return list(session.execute(select(Document).order_by(Document.id)).scalars())


def _upload_files(app) -> list[Path]:
    return [path for path in Path(app.config["DOCUMENT_UPLOAD_DIR"]).rglob("*") if path.is_file()]


# --- Service ------------------------------------------------------------------------


def test_parse_document_capture_validates_input():
    now = datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)
    capture = parse_document_capture(
        source="mobile_share",
        captured_at="2026-10-14T08:59:00+02:00",
        device="  iPad  ",
        client_sha256=PDF_SHA256.upper(),
        now=now,
    )
    assert capture.source == "mobile_share"
    assert capture.captured_at == datetime(2026, 10, 14, 6, 59, tzinfo=timezone.utc)
    assert capture.device == "iPad"
    assert capture.client_sha256 == PDF_SHA256
    assert parse_document_capture(source=None).source == "api"
    assert parse_document_capture(source="api", captured_at="2026-10-13T07:00:00Z").captured_at

    for kwargs in (
        {"source": "web"},
        {"source": "einvoice"},
        {"source": "api", "captured_at": "2026-10-14T07:00:00"},  # ohne Zeitzone
        {"source": "api", "captured_at": "gestern"},
        {"source": "api", "captured_at": (now + timedelta(minutes=11)).isoformat()},
        {"source": "api", "client_sha256": "abc"},
    ):
        with pytest.raises(DocumentCaptureError):
            parse_document_capture(now=now, **kwargs)


# --- API ----------------------------------------------------------------------------


def test_mobile_upload_stores_capture_metadata_and_audits(tmp_path):
    app, company_id, client = _setup(tmp_path)
    response = _upload(client, company_id)
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["duplicate"] is False
    assert body["file_sha256"] == PDF_SHA256
    assert body["capture_source"] == "mobile_scan"
    assert body["captured_at"] == "2026-10-13T16:15:00+00:00"
    assert body["capture_device"] == "iPhone 17 Pro"

    with app.extensions["db_session_factory"]() as session:
        audit = session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "document", AuditLog.action == "uploaded"
            )
        ).scalar_one()
    assert audit.payload["capture_source"] == "mobile_scan"
    assert audit.payload["client_hash_verified"] is True
    assert audit.payload["captured_at"] == "2026-10-13T16:15:00+00:00"

    listed = client.get(f"/api/v1/documents?company_id={company_id}").get_json()["documents"]
    assert listed[0]["capture_source"] == "mobile_scan"


def test_hash_mismatch_stores_nothing(tmp_path):
    app, company_id, client = _setup(tmp_path)
    response = _upload(client, company_id, client_sha256="0" * 64)
    assert response.status_code == 422
    body = response.get_json()
    assert body["file_sha256"] == PDF_SHA256 and body["client_sha256"] == "0" * 64
    assert _documents(app) == []
    assert _upload_files(app) == []


def test_retried_upload_returns_existing_document(tmp_path):
    app, company_id, client = _setup(tmp_path)
    first = _upload(client, company_id).get_json()
    retry = _upload(client, company_id)
    assert retry.status_code == 200
    assert retry.get_json()["id"] == first["id"]
    assert retry.get_json()["duplicate"] is True
    assert len(_documents(app)) == 1
    assert len(_upload_files(app)) == 1

    # Ohne Geräte-Hash bleibt das bisherige Verhalten: jeder Upload ist ein neuer Beleg.
    plain = _upload(
        client,
        company_id,
        client_sha256=None,
        capture_source=None,
        captured_at=None,
        capture_device=None,
    )
    assert plain.status_code == 201
    assert plain.get_json()["capture_source"] == "api"
    assert len(_documents(app)) == 2


def test_upload_rejects_invalid_capture_fields(tmp_path):
    _app, company_id, client = _setup(tmp_path)
    assert _upload(client, company_id, capture_source="web").status_code == 400
    assert _upload(client, company_id, captured_at="2026-10-13T08:15:00").status_code == 400
    assert _upload(client, company_id, client_sha256="xyz").status_code == 400


def test_multipart_upload_accepts_capture_fields(tmp_path):
    app, company_id, client = _setup(tmp_path)
    response = client.post(
        "/api/v1/documents",
        data={
            "company_id": str(company_id),
            "document_date": "2026-10-13",
            "capture_source": "mobile_share",
            "captured_at": CAPTURED_AT,
            "client_sha256": PDF_SHA256,
            "document_file": (BytesIO(PDF), "rechnung.pdf", "application/pdf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["capture_source"] == "mobile_share"
    assert _documents(app)[0].capture_source == "mobile_share"


def test_ocr_suggestion_with_mobile_capture(tmp_path):
    app, company_id, client = _setup(tmp_path)

    def ocr(**fields):
        return client.post(
            "/api/v1/receipt-ocr/suggestions",
            json={
                "company_id": company_id,
                "file_name": "scan.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(PDF).decode("ascii"),
                "document_date": "2026-10-13",
                "capture_source": "mobile_scan",
                "captured_at": CAPTURED_AT,
                "client_sha256": PDF_SHA256,
                **fields,
            },
        )

    assert ocr(client_sha256="0" * 64).status_code == 422
    assert _documents(app) == []

    created = ocr()
    assert created.status_code in (200, 201), created.get_json()
    [document] = _documents(app)
    assert document.capture_source == "mobile_scan"
    assert document.captured_at is not None

    again = ocr()
    assert again.status_code == 409
    assert again.get_json() == {
        "error": "Dieser Beleg ist bereits erfasst.",
        "duplicate": True,
        "document_id": document.id,
    }
    assert len(_documents(app)) == 1
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(ReceiptMatchSuggestion)).first() is None


# --- Weboberfläche, Prüferexport, MCP -----------------------------------------------


def test_web_upload_sets_source_and_list_shows_label(tmp_path):
    app, company_id, client = _setup(tmp_path)
    _upload(client, company_id)
    response = client.post(
        "/documents",
        data={
            "company_id": str(company_id),
            "document_date": "2026-10-12",
            "document_file": (BytesIO(PDF + b" "), "web.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code in (200, 302)
    sources = {document.file_name: document.capture_source for document in _documents(app)}
    assert sources == {"scan.pdf": "mobile_scan", "web.pdf": "web"}

    page = client.get(f"/belege?company_id={company_id}").get_data(as_text=True)
    assert "iOS-Scan" in page and "Weboberfläche" in page
    # Aufnahme 18:15 MESZ wird als UTC gespeichert, aber in Ortszeit angezeigt.
    assert "Aufgenommen 13.10.2026 18:15 auf iPhone 17 Pro" in page


def test_capture_fields_are_documented_for_export_and_mcp():
    for field in ("capture_source", "captured_at", "capture_device"):
        assert field in FIELD_DESCRIPTIONS
    by_name = {tool.name: tool for tool in TOOLS}
    for name in ("upload_document", "create_receipt_ocr_suggestion"):
        properties = by_name[name].input_schema["properties"]
        assert {"capture_source", "captured_at", "capture_device", "client_sha256"} <= set(
            properties
        )
