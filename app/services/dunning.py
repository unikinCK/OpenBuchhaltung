"""Mahnwesen (Basis): Mahnvorschläge und Mahnstufen auf offenen Forderungen.

Gemahnt werden offene debitorische Posten (``item_type = receivable``) mit
überschrittener Fälligkeit. Je Posten wird die Mahnstufe (1-3) und das
letzte Mahndatum geführt; das Mahnschreiben rendert die Weboberfläche als
druckbare Seite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import OpenItem

MAX_DUNNING_LEVEL = 3

# Mindestabstand zwischen zwei Mahnungen desselben Postens.
DUNNING_GRACE_DAYS = 7

DUNNING_LEVEL_LABELS = {
    1: "Zahlungserinnerung",
    2: "1. Mahnung",
    3: "2. Mahnung (letzte Mahnung)",
}


class DunningError(ValueError):
    """Raised when a dunning action is invalid."""


@dataclass(slots=True)
class DunningProposal:
    item: OpenItem
    days_overdue: int
    suggested_level: int

    @property
    def suggested_level_label(self) -> str:
        return DUNNING_LEVEL_LABELS[self.suggested_level]


def dunning_proposals(
    *, session: Session, company_id: int, as_of: date | None = None
) -> list[DunningProposal]:
    """Überfällige offene Forderungen, die eine (weitere) Mahnung vertragen.

    Posten mit Höchststufe oder einer Mahnung innerhalb der letzten
    ``DUNNING_GRACE_DAYS`` Tage werden nicht erneut vorgeschlagen.
    """
    as_of = as_of or date.today()
    items = (
        session.execute(
            select(OpenItem)
            .where(
                OpenItem.company_id == company_id,
                OpenItem.item_type == "receivable",
                OpenItem.status == "open",
                OpenItem.due_date.is_not(None),
                OpenItem.due_date < as_of,
            )
            .order_by(OpenItem.due_date, OpenItem.id)
        )
        .scalars()
        .all()
    )

    proposals = []
    for item in items:
        if item.dunning_level >= MAX_DUNNING_LEVEL:
            continue
        if (
            item.last_dunning_date is not None
            and (as_of - item.last_dunning_date).days < DUNNING_GRACE_DAYS
        ):
            continue
        proposals.append(
            DunningProposal(
                item=item,
                days_overdue=(as_of - item.due_date).days,
                suggested_level=item.dunning_level + 1,
            )
        )
    return proposals


def record_dunning(
    *,
    session: Session,
    open_item_id: int,
    changed_by: str,
    level: int | None = None,
    dunning_date: date | None = None,
) -> OpenItem:
    """Erhöht die Mahnstufe eines Postens (Standard: nächste Stufe) und
    protokolliert die Mahnung im Audit-Log."""
    item = session.get(OpenItem, open_item_id)
    if item is None:
        raise DunningError("Offener Posten nicht gefunden.")
    if item.item_type != "receivable":
        raise DunningError("Gemahnt werden nur debitorische Posten (Forderungen).")
    if item.status != "open":
        raise DunningError("Der Posten ist bereits ausgeglichen.")

    dunning_date = dunning_date or date.today()
    if item.due_date is None or item.due_date >= dunning_date:
        raise DunningError("Der Posten ist nicht überfällig.")

    target_level = item.dunning_level + 1 if level is None else level
    if not 1 <= target_level <= MAX_DUNNING_LEVEL:
        raise DunningError(f"Mahnstufe muss zwischen 1 und {MAX_DUNNING_LEVEL} liegen.")
    if target_level <= item.dunning_level:
        raise DunningError(
            f"Der Posten steht bereits auf Mahnstufe {item.dunning_level}."
        )

    item.dunning_level = target_level
    item.last_dunning_date = dunning_date

    log_audit_event(
        session=session,
        tenant_id=item.tenant_id,
        company_id=item.company_id,
        entity_type="open_item",
        entity_id=str(item.id),
        action="dunned",
        changed_by=changed_by,
        payload={
            "reference": item.reference,
            "dunning_level": target_level,
            "dunning_date": dunning_date.isoformat(),
            "open_amount": str(item.open_amount),
        },
    )
    session.commit()
    session.refresh(item)
    return item


def serialize_proposal(proposal: DunningProposal) -> dict[str, object]:
    item = proposal.item
    return {
        "open_item_id": item.id,
        "reference": item.reference,
        "counterparty": item.counterparty,
        "due_date": item.due_date.isoformat() if item.due_date else None,
        "days_overdue": proposal.days_overdue,
        "open_amount": str(item.open_amount),
        "currency_code": item.currency_code,
        "dunning_level": item.dunning_level,
        "last_dunning_date": (
            item.last_dunning_date.isoformat() if item.last_dunning_date else None
        ),
        "suggested_level": proposal.suggested_level,
        "suggested_level_label": proposal.suggested_level_label,
    }
