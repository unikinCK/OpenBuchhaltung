"""Prueferexport: maschinenlesbares Exportpaket mit Manifest und Hashes."""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.documents import document_file_metadata
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Company,
    DepreciationEntry,
    Document,
    ElsterSubmission,
    FiscalYear,
    FixedAsset,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    Period,
    PeriodLock,
    TaxCode,
    Tenant,
    VatReturn,
)


@dataclass(frozen=True, slots=True)
class AuditExportPackage:
    file_name: str
    payload: bytes
    manifest: dict[str, Any]


def build_audit_export_package(
    *,
    session: Session,
    company_id: int,
    generated_at: datetime,
    date_from: date | None = None,
    date_to: date | None = None,
    commit_sha: str | None = None,
    include_documents: bool = True,
) -> AuditExportPackage:
    company = session.get(Company, company_id)
    if company is None:
        raise ValueError("Company not found.")
    tenant = session.get(Tenant, company.tenant_id)

    journal_entries = _rows(
        session,
        JournalEntry,
        JournalEntry.company_id == company.id,
        _date_range(JournalEntry.entry_date, date_from, date_to),
        order_by=(JournalEntry.entry_date, JournalEntry.id),
    )
    journal_entry_ids = [entry.id for entry in journal_entries]
    journal_lines = _rows(
        session,
        JournalEntryLine,
        JournalEntryLine.journal_entry_id.in_(journal_entry_ids or [-1]),
        order_by=(JournalEntryLine.journal_entry_id, JournalEntryLine.line_number),
    )
    fiscal_years = _rows(
        session,
        FiscalYear,
        FiscalYear.company_id == company.id,
        order_by=(FiscalYear.start_date, FiscalYear.id),
    )
    fiscal_year_ids = [fiscal_year.id for fiscal_year in fiscal_years]
    periods = _rows(
        session,
        Period,
        Period.fiscal_year_id.in_(fiscal_year_ids or [-1]),
        order_by=(Period.start_date, Period.period_number),
    )
    period_ids = [period.id for period in periods]

    tables: dict[str, list[dict[str, Any]] | dict[str, Any]] = {
        "tenant": _model_dict(tenant) if tenant is not None else {},
        "company": _model_dict(company),
        "accounts": [
            _model_dict(row)
            for row in _rows(
                session, Account, Account.company_id == company.id, order_by=(Account.code,)
            )
        ],
        "tax_codes": [
            _model_dict(row)
            for row in _rows(
                session, TaxCode, TaxCode.company_id == company.id, order_by=(TaxCode.code,)
            )
        ],
        "fiscal_years": [_model_dict(row) for row in fiscal_years],
        "periods": [_model_dict(row) for row in periods],
        "period_locks": [
            _model_dict(row)
            for row in _rows(
                session,
                PeriodLock,
                PeriodLock.period_id.in_(period_ids or [-1]),
                order_by=(PeriodLock.locked_at, PeriodLock.id),
            )
        ],
        "journal_entries": [_model_dict(row) for row in journal_entries],
        "journal_entry_lines": [_model_dict(row) for row in journal_lines],
        "bank_transactions": [
            _model_dict(row)
            for row in _rows(
                session,
                BankTransaction,
                BankTransaction.company_id == company.id,
                _date_range(BankTransaction.booking_date, date_from, date_to),
                order_by=(BankTransaction.booking_date, BankTransaction.id),
            )
        ],
        "open_items": [
            _model_dict(row)
            for row in _rows(
                session,
                OpenItem,
                OpenItem.company_id == company.id,
                _date_range(OpenItem.entry_date, date_from, date_to),
                order_by=(OpenItem.entry_date, OpenItem.id),
            )
        ],
        "fixed_assets": [
            _model_dict(row)
            for row in _rows(
                session,
                FixedAsset,
                FixedAsset.company_id == company.id,
                _date_range(FixedAsset.acquisition_date, date_from, date_to),
                order_by=(FixedAsset.asset_number, FixedAsset.id),
            )
        ],
        "depreciation_entries": [
            _model_dict(row)
            for row in _rows(
                session,
                DepreciationEntry,
                DepreciationEntry.company_id == company.id,
                _date_range(DepreciationEntry.depreciation_date, date_from, date_to),
                order_by=(DepreciationEntry.depreciation_date, DepreciationEntry.id),
            )
        ],
        "vat_returns": [
            _model_dict(row)
            for row in _rows(
                session,
                VatReturn,
                VatReturn.company_id == company.id,
                VatReturn.date_to >= date_from if date_from is not None else None,
                VatReturn.date_from <= date_to if date_to is not None else None,
                order_by=(VatReturn.date_from, VatReturn.id),
            )
        ],
        "elster_submissions": [
            _model_dict(row)
            for row in _rows(
                session,
                ElsterSubmission,
                ElsterSubmission.company_id == company.id,
                order_by=(ElsterSubmission.created_at, ElsterSubmission.id),
            )
        ],
        "audit_log": [
            _model_dict(row)
            for row in _rows(
                session,
                AuditLog,
                AuditLog.company_id == company.id,
                _date_range(AuditLog.changed_at, date_from, date_to),
                order_by=(AuditLog.changed_at, AuditLog.id),
            )
        ],
    }

    documents = _rows(
        session,
        Document,
        Document.company_id == company.id,
        order_by=(Document.uploaded_at, Document.id),
    )
    document_index: list[dict[str, Any]] = []
    document_files: list[tuple[str, bytes]] = []
    for document in documents:
        document_data = _model_dict(document)
        path = Path(document.storage_key)
        archive_path = f"documents/{document.id}-{_safe_name(document.file_name)}"
        if include_documents and path.exists():
            content = path.read_bytes()
            metadata = document_file_metadata(content)
            document_data.update(
                {
                    "archive_path": archive_path,
                    "archived_file_size_bytes": len(content),
                    "archived_file_sha256": metadata.file_sha256,
                    "file_sha256_matches": (
                        metadata.file_sha256 == document.file_sha256
                        and metadata.file_size_bytes == document.file_size_bytes
                    ),
                    "file_missing": False,
                }
            )
            document_files.append((archive_path, content))
        else:
            document_data.update(
                {
                    "archive_path": archive_path if include_documents else None,
                    "archived_file_size_bytes": 0,
                    "archived_file_sha256": None,
                    "file_sha256_matches": False,
                    "file_missing": include_documents,
                }
            )
        document_index.append(document_data)
    tables["documents"] = document_index

    buffer = BytesIO()
    file_entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            _add_json(archive, file_entries, f"data/{name}.json", rows)
        for archive_path, content in document_files:
            _add_bytes(archive, file_entries, archive_path, content)

        table_counts = {
            name: len(rows) if isinstance(rows, list) else 1 for name, rows in tables.items()
        }
        manifest = {
            "export_format_version": "1",
            "generated_at": generated_at.isoformat(),
            "generator": "OpenBuchhaltung",
            "commit_sha": commit_sha,
            "parameters": {
                "company_id": company.id,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
                "include_documents": include_documents,
            },
            "company": {
                "id": company.id,
                "tenant_id": company.tenant_id,
                "name": company.name,
                "currency_code": company.currency_code,
            },
            "table_counts": table_counts,
            "files": file_entries,
            "totals": {
                "data_file_count": len(tables),
                "document_file_count": len(document_files),
                "archive_file_count": len(file_entries) + 1,
            },
        }
        _add_json(archive, [], "manifest.json", manifest)

    file_name = (
        f"prueferexport-company-{company.id}-"
        f"{generated_at.date().isoformat()}.zip"
    )
    return AuditExportPackage(
        file_name=file_name,
        payload=buffer.getvalue(),
        manifest=manifest,
    )


def _rows(session: Session, model, *filters, order_by=()):
    stmt = select(model)
    for condition in filters:
        if condition is not None:
            stmt = stmt.where(condition)
    if order_by:
        stmt = stmt.order_by(*order_by)
    return session.execute(stmt).scalars().all()


def _date_range(column, date_from: date | None, date_to: date | None):
    conditions = []
    if date_from is not None:
        conditions.append(column >= date_from)
    if date_to is not None:
        conditions.append(column <= date_to)
    if not conditions:
        return None
    condition = conditions[0]
    for next_condition in conditions[1:]:
        condition = condition & next_condition
    return condition


def _model_dict(instance) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(instance, column.name))
        for column in instance.__table__.columns
    }


def _json_value(value):
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _add_json(
    archive: zipfile.ZipFile,
    file_entries: list[dict[str, Any]],
    archive_path: str,
    payload,
) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _add_bytes(archive, file_entries, archive_path, content)


def _add_bytes(
    archive: zipfile.ZipFile,
    file_entries: list[dict[str, Any]],
    archive_path: str,
    content: bytes,
) -> None:
    archive.writestr(archive_path, content)
    file_entries.append(
        {
            "path": archive_path,
            "size_bytes": len(content),
            "sha256": _sha256(content),
        }
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_name(file_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_name).strip("._")
    return safe or "document"
