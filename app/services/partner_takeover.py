"""Altdatenübernahme: Geschäftspartner aus den Freitexten offener Posten.

Posten ohne Partner werden nach ihrem normalisierten Gegenseiten-Namen gruppiert. Die
Übernahme verknüpft eine Gruppe mit einem bestehenden Partner oder legt einen neuen an;
der Freitext bleibt unverändert. Festgeschriebene Buchungen sind unveränderlich und bleiben
ohne Partner, die Zuordnung läuft nur über die offenen Posten (Sprint 5a, Schritt 7).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.services.open_items import (
    ITEM_TYPE_SUBLEDGER,
    OpenItemError,
    assign_partner_to_item,
)
from app.services.partners import (
    NUMBER_FIELDS,
    PartnerError,
    create_partner,
    partner_display_number,
    set_partner_bank_details,
)
from domain.models import SUBLEDGER_CREDITOR, BusinessPartner, Company, OpenItem


class PartnerTakeoverError(ValueError):
    """Die Übernahme einer Gruppe ist nicht möglich."""


def _group_key(counterparty: str) -> str:
    return " ".join(counterparty.lower().split())


def _items_without_partner(session: Session, company_id: int) -> dict[str, list[OpenItem]]:
    items = session.execute(
        select(OpenItem)
        .options(selectinload(OpenItem.account))
        .where(
            OpenItem.company_id == company_id,
            OpenItem.partner_id.is_(None),
            OpenItem.counterparty.is_not(None),
        )
        .order_by(OpenItem.entry_date, OpenItem.id)
    ).scalars()
    groups: dict[str, list[OpenItem]] = {}
    for item in items:
        key = _group_key(item.counterparty or "")
        if key:
            groups.setdefault(key, []).append(item)
    return groups


def _usable(item: OpenItem) -> bool:
    return item.account.subledger == ITEM_TYPE_SUBLEDGER[item.item_type]


def _matching_partner(
    partners: list[BusinessPartner], key: str, ibans: set[str]
) -> BusinessPartner | None:
    for partner in partners:
        if _group_key(partner.name) == key:
            return partner
    for partner in partners:
        if partner.iban and partner.iban in ibans:
            return partner
    return None


def partner_takeover_proposals(*, session: Session, company_id: int) -> list[dict[str, Any]]:
    """Vorschlagsliste je Gegenseite: Posten, Beträge, IBANs und ein passender Partner."""
    partners = list(
        session.execute(
            select(BusinessPartner).where(BusinessPartner.company_id == company_id)
        ).scalars()
    )
    proposals = []
    for key, items in _items_without_partner(session, company_id).items():
        ibans = {item.counterparty_iban for item in items if item.counterparty_iban}
        match = _matching_partner(partners, key, ibans)
        spelling = Counter(item.counterparty for item in items).most_common(1)[0][0]
        proposals.append(
            {
                "key": key,
                "name": spelling,
                "item_count": len(items),
                "usable_item_count": sum(1 for item in items if _usable(item)),
                "open_total": sum(
                    (item.open_amount for item in items if item.status == "open"),
                    Decimal("0.00"),
                ),
                "is_customer": any(item.item_type == "receivable" for item in items),
                "is_supplier": any(item.item_type == "payable" for item in items),
                "ibans": sorted(ibans),
                "partner_id": match.id if match else None,
                "partner_name": match.name if match else None,
                "partner_number": partner_display_number(match) if match else None,
            }
        )
    proposals.sort(key=lambda proposal: proposal["name"].lower())
    return proposals


def serialize_takeover_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    return {**proposal, "open_total": str(proposal["open_total"])}


def _new_partner_for_group(
    *, session: Session, company: Company, items: list[OpenItem], changed_by: str
) -> BusinessPartner:
    spelling = Counter(item.counterparty for item in items).most_common(1)[0][0]
    partner = create_partner(
        session=session,
        company=company,
        changed_by=changed_by,
        name=spelling,
        is_customer=any(item.item_type == "receivable" for item in items),
        is_supplier=any(item.item_type == "payable" for item in items),
    )
    # Eine eindeutige Lieferanten-IBAN wird übernommen; bei mehreren entscheidet ein Mensch.
    ibans = {item.counterparty_iban for item in items if item.counterparty_iban}
    if len(ibans) == 1:
        iban = next(iter(ibans))
        bic = next(item.counterparty_bic for item in items if item.counterparty_iban == iban)
        set_partner_bank_details(
            session=session, partner=partner, changed_by=changed_by, iban=iban, bic=bic or ""
        )
    return partner


def apply_partner_takeover(
    *,
    session: Session,
    company_id: int,
    key: str,
    changed_by: str,
    partner_id: int | None = None,
) -> dict[str, Any]:
    """Verknüpft eine Gruppe mit dem Partner ``partner_id`` oder einem neu angelegten.

    Posten auf Konten ohne passendes Sammelkonto-Kennzeichen und Posten, zu denen der
    gewählte Partner keine Rolle hat, bleiben ohne Partner und stehen unter ``skipped``.
    """
    company = session.get(Company, company_id)
    if company is None:
        raise PartnerTakeoverError("Gesellschaft nicht gefunden.")
    items = _items_without_partner(session, company_id).get(_group_key(key or ""))
    if not items:
        raise PartnerTakeoverError("Keine offenen Posten ohne Partner zu dieser Gegenseite.")
    usable = [item for item in items if _usable(item)]
    if not usable:
        raise PartnerTakeoverError(
            "Kein Posten dieser Gegenseite liegt auf einem gekennzeichneten Sammelkonto. Das "
            "Kennzeichen wird in der Kontenpflege gesetzt."
        )

    created = partner_id is None
    try:
        if created:
            partner = _new_partner_for_group(
                session=session, company=company, items=usable, changed_by=changed_by
            )
        else:
            partner = session.get(BusinessPartner, partner_id)
            if partner is None or partner.company_id != company_id:
                raise PartnerTakeoverError("Geschäftspartner nicht gefunden.")

        linked: list[int] = []
        skipped: list[dict[str, Any]] = [
            {
                "open_item_id": item.id,
                "reference": item.reference,
                "reason": f"Konto {item.account.code} ist kein passendes Sammelkonto.",
            }
            for item in items
            if not _usable(item)
        ]
        for item in usable:
            role_field = NUMBER_FIELDS[ITEM_TYPE_SUBLEDGER[item.item_type]]
            if getattr(partner, role_field) is None:
                role = "Lieferant" if role_field == NUMBER_FIELDS[SUBLEDGER_CREDITOR] else "Kunde"
                skipped.append(
                    {
                        "open_item_id": item.id,
                        "reference": item.reference,
                        "reason": f"{partner.name} ist kein {role}.",
                    }
                )
                continue
            assign_partner_to_item(
                session=session, item=item, partner_id=partner.id, changed_by=changed_by
            )
            linked.append(item.id)
    except (OpenItemError, PartnerError) as exc:
        session.rollback()
        raise PartnerTakeoverError(str(exc)) from exc

    if not linked:
        session.rollback()
        raise PartnerTakeoverError(
            f"{partner.name} hat keine passende Rolle für die Posten dieser Gegenseite."
        )
    session.commit()
    return {
        "partner_id": partner.id,
        "partner_name": partner.name,
        "created": created,
        "linked_open_item_ids": linked,
        "skipped": skipped,
    }
