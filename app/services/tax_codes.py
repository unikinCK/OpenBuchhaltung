from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import (
    TAX_CODE_KINDS,
    TAX_KIND_INPUT,
    TAX_KIND_OUTPUT,
    Account,
    Company,
    TaxCode,
)


class TaxCodeError(ValueError):
    """Ungültige Steuercode-Definition."""


TAX_KIND_LABELS = {TAX_KIND_INPUT: "Vorsteuer", TAX_KIND_OUTPUT: "Umsatzsteuer"}


@dataclass(slots=True, frozen=True)
class DefaultTaxCode:
    code: str
    rate: Decimal
    description: str
    # Kandidaten-Kontonummern je Kontenrahmen; der erste Treffer gewinnt.
    vat_account_codes: tuple[str, ...]
    kind: str = TAX_KIND_OUTPUT


# Kontonummern für die gebündelten Kontenrahmen (SKR03, SKR04).
DEFAULT_TAX_CODES: tuple[DefaultTaxCode, ...] = (
    DefaultTaxCode("USt19", Decimal("19.00"), "Umsatzsteuer 19 %", ("1776", "3806")),
    DefaultTaxCode("USt7", Decimal("7.00"), "Umsatzsteuer 7 %", ("1771", "3801")),
    DefaultTaxCode(
        "VSt19", Decimal("19.00"), "Vorsteuer 19 %", ("1576", "1406"), TAX_KIND_INPUT
    ),
    DefaultTaxCode("VSt7", Decimal("7.00"), "Vorsteuer 7 %", ("1571", "1401"), TAX_KIND_INPUT),
    DefaultTaxCode("frei", Decimal("0.00"), "Steuerfrei", ()),
)


def derive_tax_kind(*, code: str, vat_account_type: str | None) -> str:
    """Richtung eines Steuercodes ohne explizite Angabe ableiten.

    Maßgeblich ist der Kontotyp des Steuerkontos (asset = Vorsteuer); ohne
    Steuerkonto entscheidet das Kürzel (V… = Vorsteuer).
    """
    if vat_account_type == "asset":
        return TAX_KIND_INPUT
    if vat_account_type is not None:
        return TAX_KIND_OUTPUT
    return TAX_KIND_INPUT if code.strip().lower().startswith("v") else TAX_KIND_OUTPUT


def normalize_tax_kind(
    kind: str | None, *, code: str, vat_account_type: str | None
) -> str:
    """Prüft bzw. leitet die Richtung ab und validiert sie gegen das Steuerkonto.

    Vorsteuer wird auf einem aktiven Konto (asset) gesammelt, Umsatzsteuer auf
    einem passiven (liability); ein Widerspruch würde die UStVA verdrehen.
    """
    normalized = (kind or "").strip().lower() or None
    if normalized is None:
        normalized = derive_tax_kind(code=code, vat_account_type=vat_account_type)
    if normalized not in TAX_CODE_KINDS:
        raise TaxCodeError("kind muss 'input' (Vorsteuer) oder 'output' (Umsatzsteuer) sein.")
    if vat_account_type is not None:
        expected = "asset" if normalized == TAX_KIND_INPUT else "liability"
        if vat_account_type != expected:
            raise TaxCodeError(
                f"Steuercode {code} ({TAX_KIND_LABELS[normalized]}) braucht ein "
                f"Steuerkonto der Kontoart {expected}, nicht {vat_account_type}."
            )
    return normalized


def _resolve_vat_account(
    session: Session, company: Company, candidates: tuple[str, ...]
) -> int | None:
    for code in candidates:
        account_id = session.execute(
            select(Account.id).where(
                Account.company_id == company.id, Account.code == code
            )
        ).scalar_one_or_none()
        if account_id is not None:
            return account_id
    return None


def ensure_default_tax_codes(*, session: Session, company: Company) -> int:
    """Legt fehlende Standard-Steuercodes für eine Gesellschaft an (idempotent).

    Gibt die Anzahl neu angelegter oder reparierter Steuercodes zurück.
    Bestehende Standard-Steuercodes ohne Steuerkonto werden nachträglich mit dem
    passenden Konto verknüpft, sobald es im Kontenrahmen existiert (z. B. wenn
    die Codes vor dieser Zuordnung oder mit einem anderen Kontenrahmen –
    SKR03 vs. SKR04 – angelegt wurden).
    """
    existing_by_code = {
        tax_code.code: tax_code
        for tax_code in session.execute(
            select(TaxCode).where(TaxCode.company_id == company.id)
        ).scalars()
    }

    changed = 0
    for default in DEFAULT_TAX_CODES:
        vat_account_id = _resolve_vat_account(session, company, default.vat_account_codes)

        existing = existing_by_code.get(default.code)
        if existing is not None:
            # Reparatur: Standard-Code ohne Steuerkonto nachträglich verknüpfen.
            if existing.vat_account_id is None and vat_account_id is not None:
                existing.vat_account_id = vat_account_id
                changed += 1
            continue

        session.add(
            TaxCode(
                tenant_id=company.tenant_id,
                company_id=company.id,
                code=default.code,
                rate=default.rate,
                kind=default.kind,
                description=default.description,
                vat_account_id=vat_account_id,
            )
        )
        changed += 1

    session.flush()
    return changed
