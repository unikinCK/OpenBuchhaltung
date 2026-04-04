from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Account, Company

logger = logging.getLogger(__name__)


REQUIRED_FIELDS = ("code", "name", "account_type")
CSV_FIELD_ALIASES = {
    "code": ("code", "konto", "kontonummer", "konto_nr", "account_code"),
    "name": ("name", "bezeichnung", "kontobezeichnung", "account_name"),
    "account_type": ("account_type", "kontoart", "typ", "type"),
}


@dataclass
class AccountChartImportError:
    line_number: int
    message: str


@dataclass
class AccountChartImportReport:
    total_rows: int = 0
    imported_rows: int = 0
    duplicate_rows: int = 0
    error_rows: int = 0
    errors: list[AccountChartImportError] = field(default_factory=list)


def import_account_chart_csv(
    *,
    session: Session,
    company_id: int,
    csv_stream: TextIO,
) -> AccountChartImportReport:
    """Import account chart rows from a CSV stream for one company."""
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError(f"Company with id {company_id} not found")

    reader = csv.DictReader(csv_stream)
    if not reader.fieldnames:
        raise ValueError("CSV header is missing")

    header_mapping = _resolve_header_mapping(reader.fieldnames)
    report = AccountChartImportReport()

    existing_codes = set(
        session.execute(
            select(Account.code).where(Account.company_id == company_id)
        ).scalars().all()
    )
    seen_codes = set(existing_codes)

    for line_number, raw_row in enumerate(reader, start=2):
        report.total_rows += 1
        row = {
            canonical: (raw_row.get(source) or "").strip()
            for canonical, source in header_mapping.items()
        }

        missing = [field for field in REQUIRED_FIELDS if not row[field]]
        if missing:
            _record_error(
                report,
                line_number,
                f"Pflichtfelder fehlen: {', '.join(missing)}",
            )
            continue

        if row["code"] in seen_codes:
            report.duplicate_rows += 1
            continue

        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=row["code"],
            name=row["name"],
            account_type=row["account_type"],
        )
        session.add(account)
        seen_codes.add(row["code"])
        report.imported_rows += 1

    session.commit()
    return report


def import_account_chart_file(
    *, session: Session, company_id: int, csv_path: str | Path
) -> AccountChartImportReport:
    with Path(csv_path).open("r", encoding="utf-8", newline="") as csv_stream:
        return import_account_chart_csv(
            session=session, company_id=company_id, csv_stream=csv_stream
        )


def _resolve_header_mapping(field_names: list[str]) -> dict[str, str]:
    normalized_headers = {name.strip().lower(): name for name in field_names}
    mapping: dict[str, str] = {}

    for canonical, aliases in CSV_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized_headers:
                mapping[canonical] = normalized_headers[alias]
                break

    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise ValueError(f"CSV header missing required columns: {', '.join(missing)}")

    return mapping


def _record_error(report: AccountChartImportReport, line_number: int, message: str) -> None:
    report.error_rows += 1
    report.errors.append(AccountChartImportError(line_number=line_number, message=message))
    logger.warning("Account chart import error in line %s: %s", line_number, message)
