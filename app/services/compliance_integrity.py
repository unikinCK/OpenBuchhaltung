"""Cryptographic integrity checks for finalized accounting records and documents."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import AuditIntegrityResult, verify_audit_log_integrity
from app.services.documents import verify_document_file
from domain.models import Company, Document, JournalEntry

JOURNAL_CONTENT_HASH_VERSION = 1


@dataclass(frozen=True, slots=True)
class ComplianceIntegrityIssue:
    area: str
    code: str
    message: str
    entity_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "code": self.code,
            "message": self.message,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True, slots=True)
class ComplianceIntegrityResult:
    valid: bool
    tenant_id: int | None
    company_id: int | None
    finalized_entries_checked: int
    documents_checked: int
    audit: AuditIntegrityResult
    issues: tuple[ComplianceIntegrityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "scope": {
                "tenant_id": self.tenant_id,
                "company_id": self.company_id,
                "audit_scope": "tenant" if self.tenant_id is not None else "all_tenants",
            },
            "finalized_entries_checked": self.finalized_entries_checked,
            "documents_checked": self.documents_checked,
            "audit": self.audit.as_dict(),
            "issues": [issue.as_dict() for issue in self.issues],
        }


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def calculate_journal_entry_content_hash(entry: JournalEntry) -> str:
    if not entry.is_finalized or entry.finalized_at is None or not entry.finalized_by:
        raise ValueError("Only completely finalized journal entries can be hashed.")
    if entry.content_hash_version != JOURNAL_CONTENT_HASH_VERSION:
        raise ValueError(f"Unsupported journal content hash version: {entry.content_hash_version}")

    canonical = {
        "company_id": entry.company_id,
        "content_hash_version": JOURNAL_CONTENT_HASH_VERSION,
        "created_at": _canonical_timestamp(entry.created_at),
        "description": entry.description,
        "entry_date": entry.entry_date.isoformat(),
        "finalized_at": _canonical_timestamp(entry.finalized_at),
        "finalized_by": entry.finalized_by,
        "fiscal_year_id": entry.fiscal_year_id,
        "id": entry.id,
        "lines": [
            {
                "account_id": line.account_id,
                "credit_amount": _amount(line.credit_amount),
                "currency_code": line.currency_code,
                "debit_amount": _amount(line.debit_amount),
                "description": line.description,
                "id": line.id,
                "line_number": line.line_number,
                "tax_code_id": line.tax_code_id,
                "tenant_id": line.tenant_id,
            }
            for line in sorted(entry.lines, key=lambda item: (item.line_number, item.id))
        ],
        "period_id": entry.period_id,
        "posting_number": entry.posting_number,
        "reversal_of_id": entry.reversal_of_id,
        "source": entry.source,
        "tenant_id": entry.tenant_id,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_journal_entry(entry: JournalEntry, *, finalized_at: datetime, finalized_by: str) -> None:
    # Load lines while the entry is still in its valid, open state. A lazy load
    # after changing the finalization fields could otherwise trigger an
    # autoflush before the required content hash has been assigned.
    list(entry.lines)
    entry.is_finalized = True
    entry.finalized_at = finalized_at
    entry.finalized_by = finalized_by
    entry.content_hash_version = JOURNAL_CONTENT_HASH_VERSION
    entry.content_hash = calculate_journal_entry_content_hash(entry)


def verify_compliance_integrity(
    *,
    session: Session,
    tenant_id: int | None = None,
    company_id: int | None = None,
) -> ComplianceIntegrityResult:
    if company_id is not None:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError("Company not found.")
        if tenant_id is not None and company.tenant_id != tenant_id:
            raise ValueError("Company does not belong to tenant scope.")
        effective_tenant_id = company.tenant_id
    else:
        effective_tenant_id = tenant_id

    entry_stmt = (
        select(JournalEntry)
        .where(JournalEntry.is_finalized.is_(True))
        .options(selectinload(JournalEntry.lines))
        .order_by(JournalEntry.tenant_id, JournalEntry.id)
    )
    document_stmt = select(Document).order_by(Document.tenant_id, Document.id)
    if effective_tenant_id is not None:
        entry_stmt = entry_stmt.where(JournalEntry.tenant_id == effective_tenant_id)
        document_stmt = document_stmt.where(Document.tenant_id == effective_tenant_id)
    if company_id is not None:
        entry_stmt = entry_stmt.where(JournalEntry.company_id == company_id)
        document_stmt = document_stmt.where(Document.company_id == company_id)

    entries = session.execute(entry_stmt).scalars().all()
    documents = session.execute(document_stmt).scalars().all()
    issues: list[ComplianceIntegrityIssue] = []

    for entry in entries:
        try:
            calculated_hash = calculate_journal_entry_content_hash(entry)
        except ValueError as exc:
            issues.append(
                ComplianceIntegrityIssue(
                    area="journal_entry",
                    code="unsupported_or_incomplete_hash",
                    message=str(exc),
                    entity_id=entry.id,
                )
            )
        else:
            if calculated_hash != entry.content_hash:
                issues.append(
                    ComplianceIntegrityIssue(
                        area="journal_entry",
                        code="content_hash_mismatch",
                        message="Finalized journal content does not match its stored hash.",
                        entity_id=entry.id,
                    )
                )

    for document in documents:
        file_result = verify_document_file(document)
        if not file_result["exists"]:
            issues.append(
                ComplianceIntegrityIssue(
                    area="document",
                    code="file_missing",
                    message="Archived document file is missing.",
                    entity_id=document.id,
                )
            )
        elif not file_result["matches"]:
            issues.append(
                ComplianceIntegrityIssue(
                    area="document",
                    code="file_hash_mismatch",
                    message="Archived document file does not match its stored hash and size.",
                    entity_id=document.id,
                )
            )

    audit_result = verify_audit_log_integrity(
        session=session,
        tenant_id=effective_tenant_id,
    )
    return ComplianceIntegrityResult(
        valid=audit_result.valid and not issues,
        tenant_id=effective_tenant_id,
        company_id=company_id,
        finalized_entries_checked=len(entries),
        documents_checked=len(documents),
        audit=audit_result,
        issues=tuple(issues),
    )
