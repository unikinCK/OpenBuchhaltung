from __future__ import annotations

from typing import Any

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
