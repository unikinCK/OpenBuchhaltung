from __future__ import annotations

import hashlib
import json
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import AuditLog, Tenant

AUDIT_HASH_VERSION = 1
GENESIS_HASH = "0" * 64
_SQLITE_CHAIN_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class AuditIntegrityIssue:
    code: str
    message: str
    entry_id: int | None = None
    sequence_number: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "entry_id": self.entry_id,
            "sequence_number": self.sequence_number,
        }


@dataclass(frozen=True, slots=True)
class TenantAuditIntegrityResult:
    tenant_id: int
    valid: bool
    checked_entries: int
    head_hash: str | None
    anchored_sequence_number: int
    anchored_head_hash: str
    issues: tuple[AuditIntegrityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "valid": self.valid,
            "checked_entries": self.checked_entries,
            "head_hash": self.head_hash,
            "anchored_sequence_number": self.anchored_sequence_number,
            "anchored_head_hash": self.anchored_head_hash,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class AuditIntegrityResult:
    valid: bool
    checked_entries: int
    checked_tenants: int
    tenants: tuple[TenantAuditIntegrityResult, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_entries": self.checked_entries,
            "checked_tenants": self.checked_tenants,
            "tenants": [tenant.as_dict() for tenant in self.tenants],
        }


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def calculate_audit_entry_hash(
    *,
    tenant_id: int,
    company_id: int | None,
    entity_type: str,
    entity_id: str,
    action: str,
    payload: dict[str, Any] | None,
    changed_by: str,
    changed_at: datetime,
    sequence_number: int,
    previous_hash: str,
    hash_version: int = AUDIT_HASH_VERSION,
) -> str:
    if hash_version != AUDIT_HASH_VERSION:
        raise ValueError(f"Unsupported audit hash version: {hash_version}")
    canonical = {
        "action": action,
        "changed_at": _canonical_timestamp(changed_at),
        "changed_by": changed_by,
        "company_id": company_id,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "hash_version": hash_version,
        "payload": payload,
        "previous_hash": previous_hash,
        "sequence_number": sequence_number,
        "tenant_id": tenant_id,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_for_entry(entry: AuditLog) -> str:
    return calculate_audit_entry_hash(
        tenant_id=entry.tenant_id,
        company_id=entry.company_id,
        entity_type=entry.entity_type,
        entity_id=entry.entity_id,
        action=entry.action,
        payload=entry.payload,
        changed_by=entry.changed_by,
        changed_at=entry.changed_at,
        sequence_number=entry.sequence_number,
        previous_hash=entry.previous_hash,
        hash_version=entry.hash_version,
    )


def log_audit_event(
    *,
    session: Session,
    tenant_id: int,
    company_id: int | None,
    entity_type: str,
    entity_id: str,
    action: str,
    changed_by: str,
    payload: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an event to the tenant's serialized SHA-256 audit chain."""
    session.flush()
    dialect = session.get_bind().dialect.name
    lock_context = _SQLITE_CHAIN_LOCK if dialect == "sqlite" else nullcontext()

    with lock_context:
        tenant = session.execute(
            select(Tenant)
            .where(Tenant.id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found for audit event.")

        previous_entry = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == tenant_id)
                .order_by(AuditLog.sequence_number.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        actual_sequence = previous_entry.sequence_number if previous_entry is not None else 0
        actual_head_hash = previous_entry.entry_hash if previous_entry is not None else GENESIS_HASH
        if (
            tenant.audit_sequence_number != actual_sequence
            or tenant.audit_head_hash != actual_head_hash
        ):
            raise RuntimeError(
                f"Audit chain anchor mismatch for tenant {tenant_id}; "
                "run the integrity check before appending events."
            )

        sequence_number = tenant.audit_sequence_number + 1
        previous_hash = tenant.audit_head_hash
        changed_at = datetime.now(timezone.utc)
        entry_hash = calculate_audit_entry_hash(
            tenant_id=tenant_id,
            company_id=company_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            payload=payload,
            changed_by=changed_by,
            changed_at=changed_at,
            sequence_number=sequence_number,
            previous_hash=previous_hash,
        )
        entry = AuditLog(
            tenant_id=tenant_id,
            company_id=company_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            payload=payload,
            sequence_number=sequence_number,
            hash_version=AUDIT_HASH_VERSION,
            previous_hash=previous_hash,
            entry_hash=entry_hash,
            changed_by=changed_by,
            changed_at=changed_at,
        )
        session.add(entry)
        session.flush()
        tenant.audit_sequence_number = sequence_number
        tenant.audit_head_hash = entry_hash
        session.flush()
        return entry


def serialize_audit_log_entry(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "company_id": entry.company_id,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "action": entry.action,
        "payload": entry.payload,
        "sequence_number": entry.sequence_number,
        "hash_version": entry.hash_version,
        "previous_hash": entry.previous_hash,
        "entry_hash": entry.entry_hash,
        "changed_by": entry.changed_by,
        "changed_at": entry.changed_at.isoformat(),
    }


def list_audit_log_entries(
    *,
    session: Session,
    tenant_id: int | None = None,
    company_id: int | None = None,
    entity_type: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[AuditLog]:
    limit = max(1, min(limit, 500))
    stmt = select(AuditLog)
    if tenant_id is not None:
        stmt = stmt.where(AuditLog.tenant_id == tenant_id)
    if company_id is not None:
        stmt = stmt.where(AuditLog.company_id == company_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    stmt = stmt.order_by(AuditLog.changed_at.desc(), AuditLog.id.desc()).limit(limit)
    return session.execute(stmt).scalars().all()


def verify_audit_log_integrity(
    *, session: Session, tenant_id: int | None = None
) -> AuditIntegrityResult:
    if tenant_id is None:
        tenant_ids = list(session.execute(select(Tenant.id).order_by(Tenant.id)).scalars())
    else:
        tenant_ids = list(
            session.execute(select(Tenant.id).where(Tenant.id == tenant_id)).scalars()
        )

    tenant_results: list[TenantAuditIntegrityResult] = []
    for current_tenant_id in tenant_ids:
        tenant = session.get(Tenant, current_tenant_id)
        entries = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.tenant_id == current_tenant_id)
                .order_by(AuditLog.sequence_number, AuditLog.id)
            )
            .scalars()
            .all()
        )
        issues: list[AuditIntegrityIssue] = []
        expected_previous_hash = GENESIS_HASH
        for expected_sequence, entry in enumerate(entries, start=1):
            if entry.sequence_number != expected_sequence:
                issues.append(
                    AuditIntegrityIssue(
                        code="sequence_gap",
                        message=(
                            f"Expected sequence {expected_sequence}, got {entry.sequence_number}."
                        ),
                        entry_id=entry.id,
                        sequence_number=entry.sequence_number,
                    )
                )
            if entry.previous_hash != expected_previous_hash:
                issues.append(
                    AuditIntegrityIssue(
                        code="previous_hash_mismatch",
                        message="Stored predecessor hash does not match the chain.",
                        entry_id=entry.id,
                        sequence_number=entry.sequence_number,
                    )
                )
            try:
                calculated_hash = _hash_for_entry(entry)
            except ValueError as exc:
                issues.append(
                    AuditIntegrityIssue(
                        code="unsupported_hash_version",
                        message=str(exc),
                        entry_id=entry.id,
                        sequence_number=entry.sequence_number,
                    )
                )
            else:
                if entry.entry_hash != calculated_hash:
                    issues.append(
                        AuditIntegrityIssue(
                            code="entry_hash_mismatch",
                            message="Stored entry hash does not match the audit content.",
                            entry_id=entry.id,
                            sequence_number=entry.sequence_number,
                        )
                    )
            expected_previous_hash = entry.entry_hash

        actual_sequence_number = entries[-1].sequence_number if entries else 0
        actual_head_hash = entries[-1].entry_hash if entries else GENESIS_HASH
        if tenant.audit_sequence_number != actual_sequence_number:
            issues.append(
                AuditIntegrityIssue(
                    code="anchor_sequence_mismatch",
                    message=(
                        f"Tenant anchor expects sequence {tenant.audit_sequence_number}, "
                        f"chain ends at {actual_sequence_number}."
                    ),
                    sequence_number=actual_sequence_number or None,
                )
            )
        if tenant.audit_head_hash != actual_head_hash:
            issues.append(
                AuditIntegrityIssue(
                    code="anchor_head_mismatch",
                    message="Tenant anchor does not match the stored chain head.",
                    sequence_number=actual_sequence_number or None,
                )
            )

        tenant_results.append(
            TenantAuditIntegrityResult(
                tenant_id=current_tenant_id,
                valid=not issues,
                checked_entries=len(entries),
                head_hash=entries[-1].entry_hash if entries else None,
                anchored_sequence_number=tenant.audit_sequence_number,
                anchored_head_hash=tenant.audit_head_hash,
                issues=tuple(issues),
            )
        )

    return AuditIntegrityResult(
        valid=all(result.valid for result in tenant_results),
        checked_entries=sum(result.checked_entries for result in tenant_results),
        checked_tenants=len(tenant_results),
        tenants=tuple(tenant_results),
    )
