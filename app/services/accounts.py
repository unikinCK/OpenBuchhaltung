"""Kontenstamm: Serialisierung, versionierte Änderungen und Historie."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event, serialize_audit_log_entry
from domain.models import Account, AuditLog


class AccountUpdateError(ValueError):
    """Ungültige oder leere Änderung am Kontenstamm."""


def serialize_account(account: Account) -> dict[str, Any]:
    return {
        "id": account.id,
        "tenant_id": account.tenant_id,
        "company_id": account.company_id,
        "code": account.code,
        "name": account.name,
        "account_type": account.account_type,
        "hierarchy_level": account.hierarchy_level,
        "level_1": account.level_1,
        "level_2": account.level_2,
        "level_3": account.level_3,
        "level_4": account.level_4,
        "parent_account_id": account.parent_account_id,
        "is_active": account.is_active,
    }


def log_account_created(
    *,
    session: Session,
    account: Account,
    changed_by: str,
) -> AuditLog:
    session.flush()
    return log_audit_event(
        session=session,
        tenant_id=account.tenant_id,
        company_id=account.company_id,
        entity_type="account",
        entity_id=str(account.id),
        action="created",
        changed_by=changed_by,
        payload={"before": None, "after": serialize_account(account)},
    )


def update_account_master_data(
    *,
    session: Session,
    account: Account,
    changed_by: str,
    name: str | None = None,
    is_active: bool | None = None,
) -> bool:
    if name is None and is_active is None:
        raise AccountUpdateError("At least one of name or is_active is required.")

    normalized_name = name.strip() if name is not None else account.name
    if not normalized_name:
        raise AccountUpdateError("name must not be empty.")
    if is_active is not None and not isinstance(is_active, bool):
        raise AccountUpdateError("is_active must be a boolean.")

    before = serialize_account(account)
    account.name = normalized_name
    if is_active is not None:
        account.is_active = is_active
    after = serialize_account(account)
    if before == after:
        return False

    log_audit_event(
        session=session,
        tenant_id=account.tenant_id,
        company_id=account.company_id,
        entity_type="account",
        entity_id=str(account.id),
        action="updated",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


def account_history(
    *,
    session: Session,
    account_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 500))
    entries = (
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "account",
                AuditLog.entity_id == str(account_id),
            )
            .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
            .limit(bounded_limit)
        )
        .scalars()
        .all()
    )
    return [serialize_audit_log_entry(entry) for entry in entries]
