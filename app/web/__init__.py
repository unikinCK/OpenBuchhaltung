"""UI-Blueprint "main", aufgeteilt in fachliche Routenmodule.

Die Modul-Importe registrieren die Routen am Blueprint (Import-Seiteneffekt).
"""

from __future__ import annotations

from app.web import (  # noqa: F401
    accounts,
    admin,
    bank,
    dashboard,
    documents,
    einvoice,
    fixed_assets,
    journal,
    open_items,
    periods,
    receipt_ocr,
    reports,
)
from app.web.blueprint import main_bp

__all__ = ["main_bp"]
