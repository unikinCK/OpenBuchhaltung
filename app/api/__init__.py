"""REST-API-Blueprint "api", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.api import (  # noqa: F401
    account_chart,
    accounts,
    audit_log,
    bank,
    chat,
    company_knowledge,
    controlling,
    dashboard,
    documents,
    downloads,
    einvoice,
    elster,
    exports,
    fints,
    fixed_assets,
    income_taxes,
    journal,
    journal_templates,
    knowledge,
    mcp,
    mcp_servers,
    oauth_grants,
    open_banking,
    open_items,
    partners,
    payment_runs,
    payroll,
    periods,
    push,
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
