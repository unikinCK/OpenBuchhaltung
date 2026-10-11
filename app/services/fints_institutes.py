"""FinTS-Bankenliste der Deutschen Kreditwirtschaft: Import, BLZ-Abfrage, URL-Prüfung.

Die Liste („fints_institute“, CSV mit ``;``) erhalten registrierte FinTS-Produkte
von der Registrierungsstelle; sie darf nicht weitergegeben werden und liegt
deshalb nicht im Repository. Der Betreiber importiert sie (Verwaltung, API, MCP
oder ``flask import-fints-institute``); ein Import ersetzt den gesamten Bestand.

Nutzen:

* BLZ → PIN/TAN-URL: Beim Anlegen eines Bankzugangs darf die FinTS-URL leer
  bleiben, wenn die BLZ in der Liste genau eine URL hat.
* Allowlist (Review S5): Ist eine Liste importiert, werden nur FinTS-URLs aus der
  Liste akzeptiert. Unabhängig davon sind Hosts im lokalen/privaten Netz gesperrt,
  damit ein Bankzugang nicht auf interne Dienste zeigt (SSRF) und keine PIN an einen
  fremden Server geht.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from domain.models import FinTSInstitute

logger = logging.getLogger(__name__)

MAX_IMPORT_BYTES = 10 * 1024 * 1024

# Spaltenköpfe der DK-Liste, normalisiert (klein, ohne Leerraum/Bindestriche).
_COLUMN_ALIASES = {
    "blz": "blz",
    "bic": "bic",
    "institut": "name",
    "ort": "city",
    "rz": "data_center",
    "organisation": "organisation",
    "pin/tanzugangurl": "fints_url",
    "version": "fints_version",
    "datumletzteänderung": "last_changed",
}
_REQUIRED_COLUMNS = {"blz", "name", "fints_url"}
_MAX_LENGTHS = {
    "bic": 11,
    "name": 255,
    "city": 120,
    "data_center": 120,
    "organisation": 60,
    "fints_url": 255,
    "fints_version": 40,
}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class FinTSInstituteError(ValueError):
    """Ungültige Bankenliste oder FinTS-URL."""


@dataclass(slots=True)
class FinTSInstituteImportReport:
    total_rows: int = 0
    imported_rows: int = 0
    skipped_rows: int = 0
    duplicate_rows: int = 0
    with_url: int = 0
    distinct_blz: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FinTSInstituteListStatus:
    count: int
    with_url: int
    imported_at: datetime | None


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s\-_]", "", (value or "").strip().lower())


def _decode(content: bytes) -> str:
    if content.startswith(b"PK\x03\x04"):
        raise FinTSInstituteError(
            "Excel-Dateien werden nicht unterstützt. Bitte die CSV-Fassung der "
            "Bankenliste importieren (oder in Excel „Speichern unter → CSV“)."
        )
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def normalize_fints_url(url: str) -> str:
    """Vergleichsform einer URL: Schema/Host klein, ohne abschließenden Schrägstrich."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    normalized = f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"
    if parts.query:
        normalized += f"?{parts.query}"
    return normalized


def parse_fints_institute_csv(content: bytes) -> tuple[list[dict], FinTSInstituteImportReport]:
    """Liest die DK-Bankenliste; Zeilen ohne gültige BLZ werden übersprungen."""
    if not content:
        raise FinTSInstituteError("Die Datei ist leer.")
    if len(content) > MAX_IMPORT_BYTES:
        raise FinTSInstituteError("Die Datei ist zu groß (max. 10 MB).")

    reader = csv.reader(io.StringIO(_decode(content)), delimiter=";")
    header = next(reader, None)
    if not header:
        raise FinTSInstituteError("Die Datei enthält keine Kopfzeile.")
    columns: dict[str, int] = {}
    for index, title in enumerate(header):
        key = _COLUMN_ALIASES.get(_normalize_header(title))
        if key and key not in columns:
            columns[key] = index
    missing = _REQUIRED_COLUMNS - columns.keys()
    if missing:
        raise FinTSInstituteError(
            "Das ist keine FinTS-Bankenliste: Spalten „BLZ“, „Institut“ und "
            "„PIN/TAN-Zugang URL“ werden erwartet."
        )

    report = FinTSInstituteImportReport()
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw in enumerate(reader, start=2):
        values = {
            key: (raw[index].strip() if index < len(raw) else "") for key, index in columns.items()
        }
        # Die DK-Liste endet mit nummerierten Leerzeilen („3550;;;;…“).
        if not any(values.values()):
            continue
        report.total_rows += 1
        blz = values["blz"].replace(" ", "")
        if not (blz.isdigit() and len(blz) == 8):
            report.skipped_rows += 1
            if len(report.errors) < 20:
                report.errors.append(f"Zeile {line_number}: ungültige BLZ „{values['blz']}“.")
            continue
        url = values["fints_url"]
        if not url.lower().startswith("https://") or len(url) > _MAX_LENGTHS["fints_url"]:
            url = ""
        key = (blz, normalize_fints_url(url) if url else "")
        if key in seen:
            report.duplicate_rows += 1
            continue
        seen.add(key)

        row = {
            "blz": blz,
            "bic": values.get("bic", "").replace(" ", "").upper() or None,
            "name": values["name"] or blz,
            "city": values.get("city") or None,
            "data_center": values.get("data_center") or None,
            "organisation": values.get("organisation") or None,
            "fints_url": url or None,
            "fints_version": values.get("fints_version") or None,
            "last_changed": _parse_date(values.get("last_changed", "")),
        }
        for column, max_length in _MAX_LENGTHS.items():
            if isinstance(row[column], str):
                row[column] = row[column][:max_length]
        rows.append(row)

    if not rows:
        raise FinTSInstituteError("Die Bankenliste enthält keine gültigen Einträge.")
    report.imported_rows = len(rows)
    report.with_url = sum(1 for row in rows if row["fints_url"])
    report.distinct_blz = len({row["blz"] for row in rows})
    return rows, report


def import_fints_institutes(
    *, session: Session, content: bytes, changed_by: str
) -> FinTSInstituteImportReport:
    """Ersetzt die Bankenliste vollständig durch den Inhalt der CSV."""
    rows, report = parse_fints_institute_csv(content)
    session.execute(delete(FinTSInstitute))
    session.add_all(FinTSInstitute(**row) for row in rows)
    session.commit()
    logger.info(
        "FinTS-Bankenliste importiert von %s: %s Einträge, %s mit PIN/TAN-URL",
        changed_by,
        report.imported_rows,
        report.with_url,
    )
    return report


def fints_institute_list_status(session: Session) -> FinTSInstituteListStatus:
    count, with_url, imported_at = session.execute(
        select(
            func.count(FinTSInstitute.id),
            func.count(FinTSInstitute.fints_url),
            func.max(FinTSInstitute.imported_at),
        )
    ).one()
    return FinTSInstituteListStatus(
        count=int(count or 0), with_url=int(with_url or 0), imported_at=imported_at
    )


def lookup_fints_institutes(session: Session, blz: str) -> list[FinTSInstitute]:
    """Einträge zu einer BLZ, solche mit PIN/TAN-URL zuerst."""
    blz = (blz or "").strip().replace(" ", "")
    if not (blz.isdigit() and len(blz) == 8):
        raise FinTSInstituteError("BLZ muss aus genau 8 Ziffern bestehen.")
    return list(
        session.execute(
            select(FinTSInstitute)
            .where(FinTSInstitute.blz == blz)
            .order_by(FinTSInstitute.fints_url.is_(None), FinTSInstitute.name, FinTSInstitute.id)
        ).scalars()
    )


def fints_urls_for_blz(session: Session, blz: str) -> list[str]:
    """Eindeutige PIN/TAN-URLs einer BLZ (in Listenreihenfolge)."""
    urls: list[str] = []
    seen: set[str] = set()
    for institute in lookup_fints_institutes(session, blz):
        if institute.fints_url and normalize_fints_url(institute.fints_url) not in seen:
            seen.add(normalize_fints_url(institute.fints_url))
            urls.append(institute.fints_url)
    return urls


def _host_is_internal(host: str) -> bool:
    host = host.strip("[]").lower().rstrip(".")
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES) or "." not in host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def validate_fints_url(session: Session, url: str) -> str:
    """Prüft eine FinTS-URL (https, kein internes Netz, ggf. Bankenliste)."""
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise FinTSInstituteError("FinTS-URL muss mit https:// beginnen.")
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password:
        raise FinTSInstituteError("FinTS-URL ist ungültig.")
    if _host_is_internal(parts.hostname):
        raise FinTSInstituteError("FinTS-URL darf nicht auf ein lokales oder privates Netz zeigen.")

    known = {
        normalize_fints_url(value)
        for value in session.execute(
            select(FinTSInstitute.fints_url).where(FinTSInstitute.fints_url.is_not(None)).distinct()
        ).scalars()
    }
    if known and normalize_fints_url(url) not in known:
        raise FinTSInstituteError(
            "FinTS-URL steht nicht in der importierten FinTS-Bankenliste. Bitte die URL "
            "aus der Liste verwenden (Feld leer lassen = automatisch per BLZ) oder die "
            "Bankenliste aktualisieren."
        )
    return url


def serialize_institute(institute: FinTSInstitute) -> dict:
    return {
        "blz": institute.blz,
        "bic": institute.bic,
        "name": institute.name,
        "city": institute.city,
        "data_center": institute.data_center,
        "organisation": institute.organisation,
        "fints_url": institute.fints_url,
        "fints_version": institute.fints_version,
        "last_changed": institute.last_changed.isoformat() if institute.last_changed else None,
    }


def serialize_list_status(status: FinTSInstituteListStatus) -> dict:
    return {
        "count": status.count,
        "with_url": status.with_url,
        "imported_at": status.imported_at.isoformat() if status.imported_at else None,
    }


def serialize_import_report(report: FinTSInstituteImportReport) -> dict:
    return {
        "total_rows": report.total_rows,
        "imported_rows": report.imported_rows,
        "skipped_rows": report.skipped_rows,
        "duplicate_rows": report.duplicate_rows,
        "with_url": report.with_url,
        "distinct_blz": report.distinct_blz,
        "errors": report.errors,
    }
