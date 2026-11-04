"""REST-API-Blueprint "api", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.api import (  # noqa: F401
    account_chart,
    accounts,
    audit_log,
    bank,
    cash_book,
    chat,
    company_knowledge,
    company_master_data,
    consolidation,
    controlling,
    dashboard,
    documents,
    downloads,
    einvoice,
    elster,
    exchange_rates,
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
    sales_invoices,
    system,
    tax_codes,
    tenants,
    two_factor,
    users,
    vat_returns,
)
from app.api.blueprint import api_bp

__all__ = ["api_bp"]
