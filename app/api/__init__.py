"""REST-API-Blueprint "api", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.api import (  # noqa: F401
    account_chart,
    accounts,
    audit_log,
    bank,
    documents,
    einvoice,
    elster,
    exports,
    fixed_assets,
    income_taxes,
    journal,
    mcp,
    open_items,
    payroll,
    periods,
    receipt_ocr,
    reports,
    system,
    tenants,
    users,
    vat_returns,
)
from app.api.blueprint import api_bp

__all__ = ["api_bp"]
