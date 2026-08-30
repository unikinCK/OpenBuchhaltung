"""REST-API-Blueprint "api", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.api import (  # noqa: F401
    account_chart,
    accounts,
    audit_log,
    bank,
    controlling,
    documents,
    einvoice,
    elster,
    exports,
    fints,
    fixed_assets,
    income_taxes,
    journal,
    mcp,
    open_items,
    payment_runs,
    payroll,
    periods,
    receipt_matching,
    receipt_ocr,
    reports,
    system,
    tax_codes,
    tenants,
    users,
    vat_returns,
)
from app.api.blueprint import api_bp

__all__ = ["api_bp"]
