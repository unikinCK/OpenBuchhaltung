"""REST-API-Blueprint "api", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.api import (  # noqa: F401
    accounts,
    exports,
    fixed_assets,
    journal,
    mcp,
    reports,
    system,
    tenants,
    vat_returns,
)
from app.api.blueprint import api_bp

__all__ = ["api_bp"]
