"""Kontenstamm: Serialisierung, versionierte Änderungen und Historie."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.account_hierarchy import resolve_parent_account_id
from app.services.audit_log import log_audit_event, serialize_audit_log_entry
from domain.models import (
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    SUBLEDGER_TYPES,
    Account,
    AuditLog,
    Company,
    JournalEntryLine,
)

# Kontoarten, die Berichte, Jahresabschluss und UStVA auswerten. SKR-Importe
# verwenden "income", manuell angelegte Konten häufig "revenue".
VALID_ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "revenue", "expense")

# Sammelkonten liegen auf der Bilanzseite ihrer Nebenbuchsalden.
SUBLEDGER_ACCOUNT_TYPES = {SUBLEDGER_DEBTOR: "asset", SUBLEDGER_CREDITOR: "liability"}

# Bezeichnungen, an denen der Kontenrahmen-Import die Sammelkonten erkennt.
_SUBLEDGER_NAME_PREFIXES = {
    SUBLEDGER_DEBTOR: "forderungen aus lieferungen und leistungen",
    SUBLEDGER_CREDITOR: "verbindlichkeiten aus lieferungen und leistungen",
}

_UNSET: Any = object()


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
        "subledger": account.subledger,
        "hierarchy_level": account.hierarchy_level,
        "level_1": account.level_1,
        "level_2": account.level_2,
        "level_3": account.level_3,
        "level_4": account.level_4,
        "parent_account_id": account.parent_account_id,
        "is_active": account.is_active,
    }


def validate_account_type(account_type: str) -> str:
    normalized = (account_type or "").strip()
    if normalized not in VALID_ACCOUNT_TYPES:
        raise AccountUpdateError(
            f"Unbekannter Kontotyp „{normalized}“; erlaubt sind "
            f"{', '.join(VALID_ACCOUNT_TYPES)}."
        )
    return normalized


def normalize_subledger(value: str | None, *, account_type: str) -> str | None:
    """Prüft das Sammelkonto-Kennzeichen gegen die Kontoart."""
    normalized = (value or "").strip().lower() or None
    if normalized is None:
        return None
    if normalized not in SUBLEDGER_TYPES:
        raise AccountUpdateError("subledger muss debtor, creditor oder leer sein.")
    expected_type = SUBLEDGER_ACCOUNT_TYPES[normalized]
    if account_type != expected_type:
        label = "Debitoren" if normalized == SUBLEDGER_DEBTOR else "Kreditoren"
        raise AccountUpdateError(
            f"Ein {label}-Sammelkonto muss die Kontoart {expected_type} haben."
        )
    return normalized


def default_subledger_for(*, name: str, account_type: str) -> str | None:
    """Erkennt Forderungen/Verbindlichkeiten aLuL als Sammelkonto (Kontenrahmen-Import)."""
    normalized_name = " ".join((name or "").lower().split())
    for subledger, prefix in _SUBLEDGER_NAME_PREFIXES.items():
        if (
            normalized_name.startswith(prefix)
            and account_type == SUBLEDGER_ACCOUNT_TYPES[subledger]
        ):
            return subledger
    return None


def create_account_with_audit(
    *,
    session: Session,
    company: Company,
    code: str,
    name: str,
    account_type: str,
    changed_by: str,
    subledger: str | None = None,
) -> Account:
    normalized_type = validate_account_type(account_type)
    account = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code=code,
        name=name,
        account_type=normalized_type,
        subledger=normalize_subledger(subledger, account_type=normalized_type),
        parent_account_id=resolve_parent_account_id(
            session=session, company_id=company.id, code=code
        ),
    )
    session.add(account)
    session.flush()
    log_account_created(session=session, account=account, changed_by=changed_by)
    return account


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


def _account_has_partner_lines(session: Session, account: Account) -> bool:
    return (
        session.execute(
            select(JournalEntryLine.id)
            .where(
                JournalEntryLine.account_id == account.id,
                JournalEntryLine.partner_id.is_not(None),
            )
            .limit(1)
        ).first()
        is not None
    )


def update_account_master_data(
    *,
    session: Session,
    account: Account,
    changed_by: str,
    name: str | None = None,
    is_active: bool | None = None,
    subledger: str | None | object = _UNSET,
    account_type: str | object = _UNSET,
) -> bool:
    """Ändert Bezeichnung, Aktivstatus, Sammelkonto-Kennzeichen und – nur als
    Reparatur ungültiger Altwerte – die Kontoart; jede Änderung wird mit
    vollständigem Vorher-/Nachher-Snapshot protokolliert."""
    if (
        name is None
        and is_active is None
        and subledger is _UNSET
        and account_type is _UNSET
    ):
        raise AccountUpdateError(
            "At least one of name, is_active, subledger or account_type is required."
        )

    normalized_name = name.strip() if name is not None else account.name
    if not normalized_name:
        raise AccountUpdateError("name must not be empty.")
    if is_active is not None and not isinstance(is_active, bool):
        raise AccountUpdateError("is_active must be a boolean.")

    new_type = account.account_type
    if account_type is not _UNSET:
        if not isinstance(account_type, str):
            raise AccountUpdateError("account_type must be a string.")
        requested_type = validate_account_type(account_type)
        if requested_type != account.account_type:
            if account.account_type in VALID_ACCOUNT_TYPES:
                raise AccountUpdateError(
                    "Die Kontoart ist nach der Anlage unveränderbar; nur ungültige "
                    "Altwerte dürfen korrigiert werden."
                )
            new_type = requested_type

    new_subledger = account.subledger
    if subledger is not _UNSET:
        if subledger is not None and not isinstance(subledger, str):
            raise AccountUpdateError("subledger must be a string or null.")
        new_subledger = normalize_subledger(subledger, account_type=new_type)
    elif new_subledger is not None:
        # Eine reparierte Kontoart muss weiter zum Kennzeichen passen.
        normalize_subledger(new_subledger, account_type=new_type)
    if new_subledger != account.subledger and _account_has_partner_lines(session, account):
        raise AccountUpdateError(
            f"Konto {account.code} trägt bereits Buchungszeilen mit Geschäftspartner; "
            "das Sammelkonto-Kennzeichen kann nicht mehr geändert werden."
        )

    before = serialize_account(account)
    account.name = normalized_name
    if is_active is not None:
        account.is_active = is_active
    account.account_type = new_type
    account.subledger = new_subledger
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
