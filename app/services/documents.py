"""Gemeinsame Beleg-Metadaten und Integritaetspruefungen."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from domain.models import Document


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
