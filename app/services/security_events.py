"""Sicherheitsrelevante Ereignisse: App-Log plus Audit-Hashkette.

Logins, Fehlversuche, Token-Rotation, Benutzeranlage und Passwortänderungen
werden immer in das Anwendungslog (``openbuchhaltung.security``) geschrieben.
Zusätzlich landen sie in der mandantenbezogenen Audit-Hashkette
(:func:`app.services.audit_log.log_audit_event`), sobald ein Mandant bestimmbar
ist — bei mandantengebundenen Benutzern ihr Mandant. Globale Benutzer (ohne
Mandant) haben keine Kette; für sie bleibt das Anwendungslog die Quelle.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import AuditLog, User

logger = logging.getLogger("openbuchhaltung.security")

ENTITY_TYPE_USER = "user"


def record_security_event(
    session: Session,
    *,
    user: User | None,
    action: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    tenant_id: int | None = None,
    audit: bool = True,
) -> AuditLog | None:
    """Protokolliert ein Sicherheitsereignis; liefert den Audit-Eintrag, falls angelegt.

    ``audit=False`` schreibt nur ins Anwendungslog — z. B. für Ereignisse, die
    ein Angreifer beliebig oft auslösen könnte (gesperrte Login-Versuche), damit
    die Hashkette nicht geflutet wird. Der Aufrufer committet die Session.
    """
    logger.info(
        "action=%s user=%s actor=%s payload=%s",
        action,
        user.username if user is not None else "-",
        actor,
        payload,
    )
    if not audit:
        return None
    resolved_tenant_id = tenant_id
    if resolved_tenant_id is None and user is not None:
        resolved_tenant_id = user.tenant_id
    if resolved_tenant_id is None:
        return None
    return log_audit_event(
        session=session,
        tenant_id=resolved_tenant_id,
        company_id=None,
        entity_type=ENTITY_TYPE_USER,
        entity_id=str(user.id) if user is not None else "-",
        action=action,
        changed_by=actor,
        payload=payload,
    )
