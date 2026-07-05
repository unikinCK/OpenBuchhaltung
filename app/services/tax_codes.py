from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Account, Company, TaxCode


@dataclass(slots=True, frozen=True)
class DefaultTaxCode:
    code: str
    rate: Decimal
    description: str
    vat_account_code: str | None


# Kontonummern beziehen sich auf den gebündelten SKR03-Kontenrahmen.
DEFAULT_TAX_CODES: tuple[DefaultTaxCode, ...] = (
    DefaultTaxCode("USt19", Decimal("19.00"), "Umsatzsteuer 19 %", "1776"),
    DefaultTaxCode("USt7", Decimal("7.00"), "Umsatzsteuer 7 %", "1771"),
    DefaultTaxCode("VSt19", Decimal("19.00"), "Vorsteuer 19 %", "1576"),
    DefaultTaxCode("VSt7", Decimal("7.00"), "Vorsteuer 7 %", "1571"),
    DefaultTaxCode("frei", Decimal("0.00"), "Steuerfrei", None),
)


def ensure_default_tax_codes(*, session: Session, company: Company) -> int:
    """Legt fehlende Standard-Steuercodes für eine Gesellschaft an (idempotent).

    Gibt die Anzahl neu angelegter Steuercodes zurück. Steuercodes, deren
    Steuerkonto im Kontenrahmen fehlt, werden ohne Konto angelegt und können
    erst nach Pflege des Kontos bebucht werden.
    """
    existing_codes = set(
        session.execute(
            select(TaxCode.code).where(TaxCode.company_id == company.id)
        ).scalars()
    )

    created = 0
    for default in DEFAULT_TAX_CODES:
        if default.code in existing_codes:
            continue

        vat_account_id = None
        if default.vat_account_code is not None:
            vat_account_id = session.execute(
                select(Account.id).where(
                    Account.company_id == company.id,
                    Account.code == default.vat_account_code,
                )
            ).scalar_one_or_none()

        session.add(
            TaxCode(
                tenant_id=company.tenant_id,
                company_id=company.id,
                code=default.code,
                rate=default.rate,
                description=default.description,
                vat_account_id=vat_account_id,
            )
        )
        created += 1

    session.flush()
    return created
