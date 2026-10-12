"""Belegkategorien: Standardvorauswahl, Erweiterung und Zuordnung zu Belegen."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import Company, Document, DocumentCategory

DEFAULT_DOCUMENT_CATEGORIES = (
    "Rechnung",
    "Gutschrift",
    "Zahlungsbeleg",
    "Bewirtungsbeleg",
    "Zahlungsanweisung",
)

CATEGORY_NAME_MAX_LENGTH = 80


class DocumentCategoryError(ValueError):
    """Ungültige Kategorie oder Kategorie-Zuordnung."""


def ensure_default_document_categories(*, session: Session, company: Company) -> int:
    """Legt fehlende Standardkategorien einer Gesellschaft an (idempotent).

    Gibt die Anzahl neu angelegter Kategorien zurück. Deaktivierte
    Standardkategorien bleiben deaktiviert.
    """
    existing = {
        name.casefold()
        for name in session.execute(
            select(DocumentCategory.name).where(DocumentCategory.company_id == company.id)
        ).scalars()
    }
    created = 0
    for index, name in enumerate(DEFAULT_DOCUMENT_CATEGORIES):
        if name.casefold() in existing:
            continue
        session.add(
            DocumentCategory(
                tenant_id=company.tenant_id,
                company_id=company.id,
                name=name,
                sort_order=(index + 1) * 10,
                is_default=True,
                is_active=True,
            )
        )
        created += 1
    if created:
        session.flush()
    return created


def list_document_categories(
    *, session: Session, company_id: int, include_inactive: bool = True
) -> list[DocumentCategory]:
    stmt = select(DocumentCategory).where(DocumentCategory.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(DocumentCategory.is_active.is_(True))
    stmt = stmt.order_by(DocumentCategory.sort_order, func.lower(DocumentCategory.name))
    return session.execute(stmt).scalars().all()


def find_document_category_by_name(
    *, session: Session, company_id: int, name: str
) -> DocumentCategory | None:
    """Aktive Kategorie gleichen Namens (ohne Groß-/Kleinschreibung) oder ``None``."""
    return session.execute(
        select(DocumentCategory).where(
            DocumentCategory.company_id == company_id,
            func.lower(DocumentCategory.name) == name.strip().lower(),
            DocumentCategory.is_active.is_(True),
        )
    ).scalar_one_or_none()


def create_document_category(
    *, session: Session, company: Company, name: str, changed_by: str
) -> DocumentCategory:
    """Ergänzt eine eigene Belegkategorie; Namen sind je Gesellschaft eindeutig."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise DocumentCategoryError("Bitte einen Namen für die Kategorie angeben.")
    if len(cleaned) > CATEGORY_NAME_MAX_LENGTH:
        raise DocumentCategoryError(
            f"Der Kategoriename darf höchstens {CATEGORY_NAME_MAX_LENGTH} Zeichen lang sein."
        )
    duplicate = session.execute(
        select(DocumentCategory).where(
            DocumentCategory.company_id == company.id,
            func.lower(DocumentCategory.name) == cleaned.lower(),
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        hint = "" if duplicate.is_active else " (derzeit deaktiviert)"
        raise DocumentCategoryError(f"Die Kategorie „{duplicate.name}“ existiert bereits{hint}.")

    max_sort = session.execute(
        select(func.max(DocumentCategory.sort_order)).where(
            DocumentCategory.company_id == company.id
        )
    ).scalar_one()
    category = DocumentCategory(
        tenant_id=company.tenant_id,
        company_id=company.id,
        name=cleaned,
        sort_order=(max_sort or 0) + 10,
        is_default=False,
        is_active=True,
    )
    session.add(category)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="document_category",
        entity_id=str(category.id),
        action="created",
        changed_by=changed_by,
        payload={"name": category.name},
    )
    return category


def set_document_category_active(
    *, session: Session, category: DocumentCategory, is_active: bool, changed_by: str
) -> DocumentCategory:
    """Aktiviert/deaktiviert eine Kategorie; zugeordnete Belege behalten sie."""
    if category.is_active == is_active:
        return category
    category.is_active = is_active
    log_audit_event(
        session=session,
        tenant_id=category.tenant_id,
        company_id=category.company_id,
        entity_type="document_category",
        entity_id=str(category.id),
        action="activated" if is_active else "deactivated",
        changed_by=changed_by,
        payload={"name": category.name},
    )
    return category


def resolve_document_category(
    *, session: Session, company_id: int, category_id: int | None
) -> DocumentCategory | None:
    """Prüft eine Kategorie-ID für eine Neuzuordnung (gleiche Gesellschaft, aktiv)."""
    if category_id is None:
        return None
    category = session.get(DocumentCategory, category_id)
    if category is None or category.company_id != company_id:
        raise DocumentCategoryError("Belegkategorie nicht gefunden.")
    if not category.is_active:
        raise DocumentCategoryError(f"Die Kategorie „{category.name}“ ist deaktiviert.")
    return category


def assign_document_category(
    *, session: Session, document: Document, category_id: int | None, changed_by: str
) -> Document:
    """Ordnet einem Beleg eine Kategorie zu (``None`` entfernt die Zuordnung)."""
    if category_id == document.category_id:
        return document
    category = resolve_document_category(
        session=session, company_id=document.company_id, category_id=category_id
    )
    previous_category_id = document.category_id
    document.category_id = category.id if category else None
    log_audit_event(
        session=session,
        tenant_id=document.tenant_id,
        company_id=document.company_id,
        entity_type="document",
        entity_id=str(document.id),
        action="categorized",
        changed_by=changed_by,
        payload={
            "previous_category_id": previous_category_id,
            "category_id": document.category_id,
            "category_name": category.name if category else None,
        },
    )
    return document


def document_category_dict(category: DocumentCategory) -> dict[str, object]:
    return {
        "id": category.id,
        "company_id": category.company_id,
        "name": category.name,
        "sort_order": category.sort_order,
        "is_default": category.is_default,
        "is_active": category.is_active,
    }
