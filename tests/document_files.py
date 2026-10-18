"""Realistische Beleg-Testdateien.

Der Belegupload verlangt eine Mindestgröße (DOCUMENT_MIN_UPLOAD_BYTES) und eine
zum MIME-Type passende Dateisignatur. Diese Helfer erzeugen strukturell gültige
Dateien oberhalb der Mindestgröße; ``marker`` macht den Inhalt (und damit den
SHA-256) je Test eindeutig.
"""

from __future__ import annotations

MIN_DOCUMENT_BYTES = 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff\xe0"


def _padded(prefix: bytes, suffix: bytes = b"") -> bytes:
    padding = max(0, MIN_DOCUMENT_BYTES - len(prefix) - len(suffix))
    return prefix + b"\x00" * padding + suffix


def pdf_document_bytes(marker: bytes = b"beleg") -> bytes:
    body = b"%PDF-1.4\n% " + marker + b"\n"
    padding = max(0, MIN_DOCUMENT_BYTES - len(body) - len(b"%\n%%EOF\n"))
    return body + b"%" + b"0" * padding + b"\n%%EOF\n"


def png_document_bytes(marker: bytes = b"beleg") -> bytes:
    return _padded(PNG_SIGNATURE + marker)


def jpeg_document_bytes(marker: bytes = b"beleg") -> bytes:
    return _padded(JPEG_SIGNATURE + marker, b"\xff\xd9")
