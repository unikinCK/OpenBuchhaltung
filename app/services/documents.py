"""Gemeinsame Beleg-Metadaten und Integritaetspruefungen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
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
