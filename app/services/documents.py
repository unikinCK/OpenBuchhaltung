"""Gemeinsame Beleg-Metadaten und Integritaetspruefungen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from domain.models import Document

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
