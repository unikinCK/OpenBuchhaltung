"""Gemeinsame Beleg-Metadaten und Integritaetspruefungen."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Document, DocumentCategory

# Magic Bytes je erlaubtem MIME-Type: Dateien, deren Inhalt nicht zum Typ passt
# (z. B. durch zu aggressive Client-Komprimierung zerstoerte Uploads), werden
# abgelehnt.
DOCUMENT_CONTENT_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}
DOCUMENT_SIGNATURE_PROBE_BYTES = 8


def document_content_error_code(
    *,
    mime_type: str,
    content_head: bytes,
    content_size: int,
    max_bytes: int | None,
    min_bytes: int | None,
) -> str | None:
    """Prueft Groesse und Dateisignatur eines Beleg-Uploads.

    Liefert einen Fehlercode ("too_large", "too_small", "signature_mismatch")
    oder ``None``, wenn der Inhalt plausibel ist.
    """
    if max_bytes and content_size > max_bytes:
        return "too_large"
    if min_bytes and content_size < min_bytes:
        return "too_small"
    signatures = DOCUMENT_CONTENT_SIGNATURES.get(mime_type)
    if signatures and not any(content_head.startswith(sig) for sig in signatures):
        return "signature_mismatch"
    return None


@dataclass(frozen=True, slots=True)
class DocumentFileMetadata:
    file_sha256: str
    file_size_bytes: int


# Erfassungswege eines Belegs (``Document.capture_source``). Über die API wählbar sind
# nur ``API_CAPTURE_SOURCES``; ``web`` und ``einvoice`` setzt die Weboberfläche selbst.
CAPTURE_SOURCE_LABELS = {
    "web": "Weboberfläche",
    "api": "API",
    "einvoice": "E-Rechnung",
    "mobile_scan": "iOS-Scan",
    "mobile_share": "iOS-Teilen",
}
API_CAPTURE_SOURCES = ("api", "mobile_scan", "mobile_share")
MOBILE_CAPTURE_SOURCES = ("mobile_scan", "mobile_share")
# Toleranz für Uhrabweichungen des Geräts beim Aufnahmezeitpunkt.
CAPTURED_AT_FUTURE_TOLERANCE = timedelta(minutes=10)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DocumentCaptureError(ValueError):
    """Ungültige Angaben zur Belegerfassung (Quelle, Zeitpunkt, Gerät, Hash)."""


@dataclass(frozen=True, slots=True)
class DocumentCapture:
    source: str
    captured_at: datetime | None = None
    device: str | None = None
    client_sha256: str | None = None


def parse_document_capture(
    *,
    source: object,
    captured_at: object = None,
    device: object = None,
    client_sha256: object = None,
    now: datetime | None = None,
) -> DocumentCapture:
    """Prüft die Erfassungsangaben eines API-Uploads.

    ``captured_at`` muss eine ISO-8601-Zeit mit Zeitzone sein und darf höchstens
    zehn Minuten in der Zukunft liegen; ``client_sha256`` ist der auf dem Gerät
    berechnete SHA-256 der Datei (64 Hex-Zeichen).
    """
    source = str(source or "api").strip()
    if source not in API_CAPTURE_SOURCES:
        raise DocumentCaptureError(
            f"capture_source must be one of: {', '.join(API_CAPTURE_SOURCES)}."
        )
    parsed_at = None
    if captured_at not in (None, ""):
        try:
            parsed_at = datetime.fromisoformat(str(captured_at).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise DocumentCaptureError("captured_at must be an ISO 8601 timestamp.") from exc
        if parsed_at.tzinfo is None:
            raise DocumentCaptureError("captured_at must include a time zone.")
        now = now or datetime.now(timezone.utc)
        if parsed_at > now + CAPTURED_AT_FUTURE_TOLERANCE:
            raise DocumentCaptureError("captured_at lies in the future.")
        parsed_at = parsed_at.astimezone(timezone.utc)
    device_name = None
    if device is not None:
        device_name = str(device).strip()[:100] or None
    client_hash = None
    if client_sha256 not in (None, ""):
        client_hash = str(client_sha256).strip().lower()
        if not _SHA256_RE.match(client_hash):
            raise DocumentCaptureError("client_sha256 must be a hex SHA-256 digest.")
    return DocumentCapture(
        source=source, captured_at=parsed_at, device=device_name, client_sha256=client_hash
    )


def find_current_document_by_hash(
    session: Session, *, company_id: int, file_sha256: str
) -> Document | None:
    """Aktueller Beleg der Gesellschaft mit diesem Inhalt (für idempotente Uploads)."""
    return (
        session.execute(
            select(Document)
            .where(
                Document.company_id == company_id,
                Document.file_sha256 == file_sha256,
                Document.is_current.is_(True),
            )
            .order_by(Document.id)
        )
        .scalars()
        .first()
    )


def document_file_metadata(content: bytes) -> DocumentFileMetadata:
    return DocumentFileMetadata(
        file_sha256=hashlib.sha256(content).hexdigest(),
        file_size_bytes=len(content),
    )


def verify_document_file(document: Document) -> dict[str, object]:
    path = Path(document.storage_key)
    if not path.exists():
        return {
            "exists": False,
            "matches": False,
            "actual_sha256": None,
            "actual_size_bytes": 0,
        }

    content = path.read_bytes()
    metadata = document_file_metadata(content)
    return {
        "exists": True,
        "matches": (
            metadata.file_sha256 == document.file_sha256
            and metadata.file_size_bytes == document.file_size_bytes
        ),
        "actual_sha256": metadata.file_sha256,
        "actual_size_bytes": metadata.file_size_bytes,
    }


def documents_by_journal_entry(
    session: Session, journal_entry_ids: list[int]
) -> dict[int, list[dict[str, object]]]:
    """Aktuelle Belegversionen je Buchung (eine Buchung kann mehrere Belege haben)."""
    if not journal_entry_ids:
        return {}
    rows = session.execute(
        select(
            Document.id,
            Document.journal_entry_id,
            Document.file_name,
            Document.document_date,
            Document.category_id,
            DocumentCategory.name.label("category_name"),
        )
        .outerjoin(DocumentCategory, DocumentCategory.id == Document.category_id)
        .where(
            Document.journal_entry_id.in_(journal_entry_ids),
            Document.is_current.is_(True),
        )
        .order_by(Document.journal_entry_id, Document.document_date, Document.id)
    ).all()
    result: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        result.setdefault(row.journal_entry_id, []).append(
            {
                "id": row.id,
                "file_name": row.file_name,
                "document_date": row.document_date,
                "category_id": row.category_id,
                "category_name": row.category_name,
            }
        )
    return result
