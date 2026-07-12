from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import AuditLog


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
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            company_id=company_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            payload=payload,
            changed_by=changed_by,
        )
    )


def serialize_audit_log_entry(entry: AuditLog) -> dict[str, Any]:
    return {
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "company_id": entry.company_id,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "action": entry.action,
        "payload": entry.payload,
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
