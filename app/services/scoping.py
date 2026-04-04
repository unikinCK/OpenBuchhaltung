from __future__ import annotations

from sqlalchemy import Select, select


def scoped_select(model: type, *, tenant_id: int | None = None, company_id: int | None = None) -> Select:
    """Build a reusable select with optional tenant/company filters."""
    stmt = select(model)

    if tenant_id is not None and hasattr(model, "tenant_id"):
        stmt = stmt.where(getattr(model, "tenant_id") == tenant_id)

    if company_id is not None and hasattr(model, "company_id"):
        stmt = stmt.where(getattr(model, "company_id") == company_id)

    return stmt
