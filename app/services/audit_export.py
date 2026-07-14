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
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.compliance_integrity import verify_compliance_integrity
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
    IncomeTaxReturn,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    PayrollEmployee,
    PayrollRun,
    PayrollRunLine,
    Period,
    PeriodLock,
    TaxCode,
    Tenant,
    User,
    VatReturn,
)


@dataclass(frozen=True, slots=True)
class AuditExportPackage:
    file_name: str
    payload: bytes
    manifest: dict[str, Any]
    package_sha256: str


@dataclass(frozen=True, slots=True)
class AuditExportVerificationIssue:
    code: str
    message: str
    path: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "path": self.path}


@dataclass(frozen=True, slots=True)
class AuditExportVerificationResult:
    valid: bool
    package_sha256: str
    dataset_sha256: str | None
    files_checked: int
    issues: tuple[AuditExportVerificationIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "package_sha256": self.package_sha256,
            "dataset_sha256": self.dataset_sha256,
            "files_checked": self.files_checked,
            "issues": [issue.as_dict() for issue in self.issues],
        }


EXPORT_TABLE_MODELS = {
    "tenant": Tenant,
    "company": Company,
    "accounts": Account,
    "tax_codes": TaxCode,
    "fiscal_years": FiscalYear,
    "periods": Period,
    "period_locks": PeriodLock,
    "journal_entries": JournalEntry,
    "journal_entry_lines": JournalEntryLine,
    "bank_transactions": BankTransaction,
    "open_items": OpenItem,
    "fixed_assets": FixedAsset,
    "depreciation_entries": DepreciationEntry,
    "payroll_employees": PayrollEmployee,
    "payroll_runs": PayrollRun,
    "payroll_run_lines": PayrollRunLine,
    "vat_returns": VatReturn,
    "income_tax_returns": IncomeTaxReturn,
    "elster_submissions": ElsterSubmission,
    "audit_log": AuditLog,
    "account_history": AuditLog,
    "documents": Document,
    "users": User,
}

TABLE_DESCRIPTIONS = {
    "tenant": "Mandantenstammdaten und Audit-Kettenanker.",
    "company": "Gesellschaftsstammdaten des Exportumfangs.",
    "accounts": "Kontenstamm der Gesellschaft.",
    "tax_codes": "Steuerschlüssel der Gesellschaft.",
    "fiscal_years": "Geschäftsjahre der Gesellschaft.",
    "periods": "Buchungsperioden der exportierten Geschäftsjahre.",
    "period_locks": "Sperrprotokolle der exportierten Perioden.",
    "journal_entries": "Buchungsköpfe einschließlich Festschreibung und Inhaltshash.",
    "journal_entry_lines": "Soll- und Haben-Zeilen der exportierten Buchungen.",
    "bank_transactions": "Importierte Bankumsätze und Buchungszuordnungen.",
    "open_items": "Offene und ausgeglichene Forderungen und Verbindlichkeiten.",
    "fixed_assets": "Anlagenstamm und Bewertungsparameter.",
    "depreciation_entries": "Gebuchte planmäßige und außerplanmäßige Abschreibungen.",
    "payroll_employees": "Prüfungsrelevante Beschäftigten- und Abrechnungsstammdaten.",
    "payroll_runs": "Lohnabrechnungsläufe und Summen.",
    "payroll_run_lines": "Beschäftigtenbezogene Ergebnisse der Lohnabrechnungsläufe.",
    "vat_returns": "Festgehaltene Umsatzsteuer-Voranmeldungen und Jahreserklärungen.",
    "income_tax_returns": "Festgehaltene Körperschaft- und Gewerbesteuerberechnungen.",
    "elster_submissions": "ELSTER-Nutzdaten und Übermittlungsprotokolle.",
    "audit_log": "Gesellschaftsbezogene Ausschnitte des verketteten Audit-Logs.",
    "account_history": (
        "Kontenstamm-Historie mit unveränderbaren Vorher-/Nachher-Snapshots."
    ),
    "documents": "Belegindex, Versionen und Prüfsummen der Originaldateien.",
    "users": "Mandantenbezogene Benutzer und Rollen ohne Authentisierungsgeheimnisse.",
}

FIELD_DESCRIPTIONS = {
    "id": "Eindeutiger Primärschlüssel des Datensatzes.",
    "tenant_id": "Mandantenzuordnung des Datensatzes.",
    "company_id": "Gesellschaftszuordnung des Datensatzes.",
    "journal_entry_id": "Verknüpfte Journalbuchung.",
    "created_at": "Zeitpunkt der erstmaligen Erzeugung.",
    "created_by": "Benutzer oder Prozess, der den Datensatz erzeugt hat.",
    "is_finalized": "Kennzeichnet eine unveränderbar festgeschriebene Buchung.",
    "finalized_at": "Zeitpunkt der Festschreibung.",
    "finalized_by": "Benutzer oder Prozess der Festschreibung.",
    "content_hash": "SHA-256-Inhaltshash der festgeschriebenen Buchung.",
    "content_hash_version": "Version des Verfahrens für den Buchungsinhaltshash.",
    "file_sha256": "Beim Eingang gespeicherter SHA-256-Hash der Originaldatei.",
    "file_size_bytes": "Beim Eingang gespeicherte Dateigröße in Bytes.",
    "document_date": "Fachliches Datum des Belegs, getrennt vom Erfassungszeitpunkt.",
    "archive_path": "Relativer Pfad der Originaldatei innerhalb des ZIP-Pakets.",
    "archived_file_sha256": "Beim Export berechneter SHA-256-Hash der Originaldatei.",
    "archived_file_size_bytes": "Beim Export ermittelte Dateigröße in Bytes.",
    "file_sha256_matches": "Ergebnis des Vergleichs von gespeichertem und exportiertem Hash.",
    "file_missing": "Kennzeichnet eine zum Exportzeitpunkt fehlende Originaldatei.",
    "file_included": "Kennzeichnet, ob die Originaldatei im ZIP-Paket enthalten ist.",
    "api_token_configured": "Kennzeichnet, ob ein API-Token hinterlegt ist; kein Geheimniswert.",
    "sequence_number": "Fortlaufende Sequenznummer innerhalb der Mandanten-Auditkette.",
    "previous_hash": "SHA-256-Hash des vorherigen Audit-Eintrags.",
    "entry_hash": "SHA-256-Inhaltshash dieses Audit-Eintrags.",
}

DOCUMENT_DERIVED_FIELDS = {
    "archive_path": ("string", True),
    "archived_file_size_bytes": ("integer", True),
    "archived_file_sha256": ("string", True),
    "file_sha256_matches": ("boolean", True),
    "file_missing": ("boolean", True),
    "file_included": ("boolean", False),
}

USER_DERIVED_FIELDS = {
    "api_token_configured": ("boolean", False),
}

EXCLUDED_EXPORT_FIELDS = {
    "documents": {"storage_key"},
    "users": {"password_hash", "api_token_hash", "api_token_last4"},
}

EXPORT_README = (
    "OpenBuchhaltung Prüferexport\n\n"
    "Das Paket enthält maschinenlesbare JSON-Daten unter data/, einen vollständigen\n"
    "Feldkatalog unter schema/field_catalog.json und optional Originalbelege unter\n"
    "documents/. manifest.json enthält Exportparameter, Dateigrößen, SHA-256-Hashes\n"
    "und den dataset_sha256-Gesamtnachweis über alle dort aufgeführten Dateien.\n"
    "Der ZIP-Hash wird außerhalb des Pakets über API-Header oder CLI ausgegeben.\n"
)


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
    integrity_result = verify_compliance_integrity(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
    )

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
    payroll_runs = _rows(
        session,
        PayrollRun,
        PayrollRun.company_id == company.id,
        _date_range(PayrollRun.payment_date, date_from, date_to),
        order_by=(PayrollRun.payment_date, PayrollRun.id),
    )
    payroll_run_ids = [run.id for run in payroll_runs]

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
        "payroll_employees": [
            _model_dict(row)
            for row in _rows(
                session,
                PayrollEmployee,
                PayrollEmployee.company_id == company.id,
                order_by=(PayrollEmployee.last_name, PayrollEmployee.first_name),
            )
        ],
        "payroll_runs": [_model_dict(row) for row in payroll_runs],
        "payroll_run_lines": [
            _model_dict(row)
            for row in _rows(
                session,
                PayrollRunLine,
                PayrollRunLine.payroll_run_id.in_(payroll_run_ids or [-1]),
                order_by=(PayrollRunLine.payroll_run_id, PayrollRunLine.id),
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
        "income_tax_returns": [
            _model_dict(row)
            for row in _rows(
                session,
                IncomeTaxReturn,
                IncomeTaxReturn.company_id == company.id,
                IncomeTaxReturn.date_to >= date_from if date_from is not None else None,
                IncomeTaxReturn.date_from <= date_to if date_to is not None else None,
                order_by=(IncomeTaxReturn.date_from, IncomeTaxReturn.id),
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
        "account_history": [
            _model_dict(row)
            for row in _rows(
                session,
                AuditLog,
                AuditLog.company_id == company.id,
                AuditLog.entity_type == "account",
                order_by=(AuditLog.changed_at, AuditLog.id),
            )
        ],
        "users": [
            _user_export_dict(row)
            for row in _rows(
                session,
                User,
                User.tenant_id == company.tenant_id,
                order_by=(User.username, User.id),
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
        document_data = _document_export_dict(document)
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
                    "file_included": True,
                }
            )
            document_files.append((archive_path, content))
        else:
            document_data.update(
                {
                    "archive_path": archive_path if include_documents else None,
                    "archived_file_size_bytes": 0 if include_documents else None,
                    "archived_file_sha256": None,
                    "file_sha256_matches": False if include_documents else None,
                    "file_missing": True if include_documents else None,
                    "file_included": False,
                }
            )
        document_index.append(document_data)
    tables["documents"] = document_index
    field_catalog = _build_field_catalog(tables)

    buffer = BytesIO()
    file_entries: list[dict[str, Any]] = []
    zip_timestamp = _zip_timestamp(generated_at)
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in tables.items():
            _add_json(
                archive,
                file_entries,
                f"data/{name}.json",
                rows,
                zip_timestamp=zip_timestamp,
            )
        _add_json(
            archive,
            file_entries,
            "schema/field_catalog.json",
            field_catalog,
            zip_timestamp=zip_timestamp,
        )
        _add_bytes(
            archive,
            file_entries,
            "README.txt",
            EXPORT_README.encode("utf-8"),
            zip_timestamp=zip_timestamp,
        )
        for archive_path, content in document_files:
            _add_bytes(
                archive,
                file_entries,
                archive_path,
                content,
                zip_timestamp=zip_timestamp,
            )

        table_counts = {
            name: len(rows) if isinstance(rows, list) else 1 for name, rows in tables.items()
        }
        dataset_sha256 = _file_set_sha256(file_entries)
        manifest = {
            "export_format_version": "2",
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
            "scope_notes": {
                "dated_transaction_tables": "date_from/date_to",
                "master_and_support_tables": "company-wide",
                "audit_log": "company-filtered; integrity snapshot is tenant-wide",
            },
            "integrity": integrity_result.as_dict(),
            "field_catalog_path": "schema/field_catalog.json",
            "dataset_sha256": {
                "algorithm": "sha256",
                "value": dataset_sha256,
                "covers": "canonical path, size_bytes and sha256 of every manifest file",
            },
            "table_counts": table_counts,
            "files": file_entries,
            "totals": {
                "data_file_count": len(tables),
                "document_file_count": len(document_files),
                "archive_file_count": len(file_entries) + 1,
            },
        }
        _add_json(
            archive,
            [],
            "manifest.json",
            manifest,
            zip_timestamp=zip_timestamp,
        )

    file_name = (
        f"prueferexport-company-{company.id}-"
        f"{generated_at.date().isoformat()}.zip"
    )
    payload = buffer.getvalue()
    return AuditExportPackage(
        file_name=file_name,
        payload=payload,
        manifest=manifest,
        package_sha256=_sha256(payload),
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


def _document_export_dict(document: Document) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(document, column.name))
        for column in document.__table__.columns
        if column.name not in EXCLUDED_EXPORT_FIELDS["documents"]
    }


def _user_export_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": _json_value(user.created_at),
        "api_token_configured": user.api_token_hash is not None,
    }


def _build_field_catalog(
    tables: dict[str, list[dict[str, Any]] | dict[str, Any]],
) -> dict[str, Any]:
    catalog: dict[str, Any] = {
        "schema_format_version": "1",
        "description": "Technischer Feldkatalog des OpenBuchhaltung-Prüferexports.",
        "tables": {},
    }
    for export_name in tables:
        model = EXPORT_TABLE_MODELS[export_name]
        excluded = EXCLUDED_EXPORT_FIELDS.get(export_name, set())
        fields = [
            _column_catalog_entry(export_name, column)
            for column in model.__table__.columns
            if column.name not in excluded
        ]
        derived_fields = (
            DOCUMENT_DERIVED_FIELDS
            if export_name == "documents"
            else USER_DERIVED_FIELDS if export_name == "users" else {}
        )
        for name, (json_type, nullable) in derived_fields.items():
            fields.append(
                {
                    "name": name,
                    "json_type": json_type,
                    "sql_type": None,
                    "nullable": nullable,
                    "primary_key": False,
                    "foreign_keys": [],
                    "description_de": FIELD_DESCRIPTIONS[name],
                    "derived": True,
                }
            )
        catalog["tables"][export_name] = {
            "path": f"data/{export_name}.json",
            "shape": "object" if export_name in {"tenant", "company"} else "array",
            "source_table": model.__tablename__,
            "description_de": TABLE_DESCRIPTIONS[export_name],
            "fields": fields,
        }
    return catalog


def _column_catalog_entry(export_name: str, column) -> dict[str, Any]:
    foreign_keys = sorted(foreign_key.target_fullname for foreign_key in column.foreign_keys)
    return {
        "name": column.name,
        "json_type": _column_json_type(column),
        "sql_type": str(column.type),
        "nullable": column.nullable,
        "primary_key": column.primary_key,
        "foreign_keys": foreign_keys,
        "description_de": _field_description(export_name, column.name, foreign_keys),
        "derived": False,
    }


def _column_json_type(column) -> str:
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        return "object"
    if python_type is bool:
        return "boolean"
    if python_type is int:
        return "integer"
    if python_type is float:
        return "number"
    if python_type in {Decimal, date, datetime, str}:
        return "string"
    if python_type is list:
        return "array"
    return "object"


def _field_description(export_name: str, name: str, foreign_keys: list[str]) -> str:
    if name in FIELD_DESCRIPTIONS:
        return FIELD_DESCRIPTIONS[name]
    if foreign_keys:
        return f"Fremdschlüssel auf {', '.join(foreign_keys)}."
    label = name.replace("_", " ")
    return f"Fach- oder Statusfeld „{label}“ in {TABLE_DESCRIPTIONS[export_name].rstrip('.')}."


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
    *,
    zip_timestamp: tuple[int, int, int, int, int, int],
) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _add_bytes(
        archive,
        file_entries,
        archive_path,
        content,
        zip_timestamp=zip_timestamp,
    )


def _add_bytes(
    archive: zipfile.ZipFile,
    file_entries: list[dict[str, Any]],
    archive_path: str,
    content: bytes,
    *,
    zip_timestamp: tuple[int, int, int, int, int, int],
) -> None:
    info = zipfile.ZipInfo(archive_path, date_time=zip_timestamp)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    archive.writestr(info, content)
    file_entries.append(
        {
            "path": archive_path,
            "size_bytes": len(content),
            "sha256": _sha256(content),
        }
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_set_sha256(file_entries: list[dict[str, Any]]) -> str:
    canonical_entries = sorted(
        (
            {
                "path": entry["path"],
                "size_bytes": entry["size_bytes"],
                "sha256": entry["sha256"],
            }
            for entry in file_entries
        ),
        key=lambda entry: entry["path"],
    )
    content = json.dumps(
        canonical_entries,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(content)


def _zip_timestamp(generated_at: datetime) -> tuple[int, int, int, int, int, int]:
    values = generated_at.utctimetuple()[:6]
    year = min(2107, max(1980, values[0]))
    return (year, *values[1:])


def verify_audit_export_package(payload: bytes) -> AuditExportVerificationResult:
    package_sha256 = _sha256(payload)
    issues: list[AuditExportVerificationIssue] = []
    actual_entries: list[dict[str, Any]] = []
    files_checked = 0
    dataset_sha256: str | None = None

    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except (zipfile.BadZipFile, OSError) as exc:
        return AuditExportVerificationResult(
            valid=False,
            package_sha256=package_sha256,
            dataset_sha256=None,
            files_checked=0,
            issues=(
                AuditExportVerificationIssue(
                    code="invalid_zip",
                    message=f"ZIP package cannot be read: {exc}",
                ),
            ),
        )

    with archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            issues.append(
                AuditExportVerificationIssue(
                    code="duplicate_path",
                    message="ZIP package contains duplicate paths.",
                )
            )
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except KeyError:
            issues.append(
                AuditExportVerificationIssue(
                    code="manifest_missing",
                    message="manifest.json is missing.",
                    path="manifest.json",
                )
            )
            manifest = None
        except (
            zipfile.BadZipFile,
            json.JSONDecodeError,
            UnicodeDecodeError,
            RuntimeError,
        ) as exc:
            issues.append(
                AuditExportVerificationIssue(
                    code="manifest_invalid",
                    message=f"manifest.json is invalid: {exc}",
                    path="manifest.json",
                )
            )
            manifest = None

        declared_files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(declared_files, list):
            if manifest is not None:
                issues.append(
                    AuditExportVerificationIssue(
                        code="file_index_invalid",
                        message="Manifest file index is not a list.",
                        path="manifest.json",
                    )
                )
            declared_files = []

        declared_paths: set[str] = set()
        for entry in declared_files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                issues.append(
                    AuditExportVerificationIssue(
                        code="file_entry_invalid",
                        message="Manifest contains an invalid file entry.",
                        path="manifest.json",
                    )
                )
                continue
            path = entry["path"]
            if path in declared_paths:
                issues.append(
                    AuditExportVerificationIssue(
                        code="duplicate_manifest_path",
                        message="Manifest declares the same path more than once.",
                        path=path,
                    )
                )
            declared_paths.add(path)
            pure_path = PurePosixPath(path)
            if pure_path.is_absolute() or ".." in pure_path.parts:
                issues.append(
                    AuditExportVerificationIssue(
                        code="unsafe_path",
                        message="Manifest contains an unsafe archive path.",
                        path=path,
                    )
                )
            try:
                content = archive.read(path)
            except KeyError:
                issues.append(
                    AuditExportVerificationIssue(
                        code="file_missing",
                        message="File declared in manifest is missing.",
                        path=path,
                    )
                )
                continue
            except (zipfile.BadZipFile, RuntimeError) as exc:
                issues.append(
                    AuditExportVerificationIssue(
                        code="file_unreadable",
                        message=f"Declared file cannot be read: {exc}",
                        path=path,
                    )
                )
                continue
            files_checked += 1
            actual_entry = {
                "path": path,
                "size_bytes": len(content),
                "sha256": _sha256(content),
            }
            actual_entries.append(actual_entry)
            if entry.get("size_bytes") != actual_entry["size_bytes"]:
                issues.append(
                    AuditExportVerificationIssue(
                        code="size_mismatch",
                        message="Actual file size does not match manifest.",
                        path=path,
                    )
                )
            if entry.get("sha256") != actual_entry["sha256"]:
                issues.append(
                    AuditExportVerificationIssue(
                        code="hash_mismatch",
                        message="Actual file hash does not match manifest.",
                        path=path,
                    )
                )

        unexpected_paths = set(names) - declared_paths - {"manifest.json"}
        for path in sorted(unexpected_paths):
            issues.append(
                AuditExportVerificationIssue(
                    code="unexpected_file",
                    message="ZIP package contains a file not declared in manifest.",
                    path=path,
                )
            )

        if declared_files:
            dataset_sha256 = _file_set_sha256(actual_entries)
            expected_dataset = manifest.get("dataset_sha256", {})
            expected_value = (
                expected_dataset.get("value") if isinstance(expected_dataset, dict) else None
            )
            if expected_value != dataset_sha256:
                issues.append(
                    AuditExportVerificationIssue(
                        code="dataset_hash_mismatch",
                        message="Dataset hash does not match the manifest file set.",
                        path="manifest.json",
                    )
                )

    return AuditExportVerificationResult(
        valid=not issues,
        package_sha256=package_sha256,
        dataset_sha256=dataset_sha256,
        files_checked=files_checked,
        issues=tuple(issues),
    )


def _safe_name(file_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", file_name).strip("._")
    return safe or "document"
