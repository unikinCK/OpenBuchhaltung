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


# Steuerkonten je Kontenrahmen: SKR03 1776/1771 (USt) und 1576/1571 (VSt),
# SKR04 3806/3801 (USt) und 1406/1401 (VSt). Die Suche nach der Nummer allein
# ist hier eindeutig: Laut DATEV-Kontenrahmen 2026 sind die SKR03-Nummern im
# SKR04 nicht belegt und umgekehrt (SKR03 1401–1406 sind reserviert).
DEFAULT_TAX_CODES: tuple[DefaultTaxCode, ...] = (
    DefaultTaxCode("USt19", Decimal("19.00"), "Umsatzsteuer 19 %", ("1776", "3806")),
    DefaultTaxCode("USt7", Decimal("7.00"), "Umsatzsteuer 7 %", ("1771", "3801")),
    DefaultTaxCode(
        "VSt19", Decimal("19.00"), "Vorsteuer 19 %", ("1576", "1406"), TAX_KIND_INPUT
    ),
    DefaultTaxCode("VSt7", Decimal("7.00"), "Vorsteuer 7 %", ("1571", "1401"), TAX_KIND_INPUT),
    DefaultTaxCode("frei", Decimal("0.00"), "Steuerfrei", ()),
)

# Die Standard-Steuerkonten beider Kontenrahmen gelten auch ohne Steuercode als
# Steuerkonten (Gesellschaften ohne Steuercodes, Altbuchungen auf den Konten des
# anderen Kontenrahmens wie SKR04 3806 in einer SKR03-Buchhaltung).
STANDARD_TAX_ACCOUNTS: dict[str, tuple[str, Decimal]] = {
    code: (default.kind, default.rate)
    for default in DEFAULT_TAX_CODES
    if default.rate > Decimal("0.00")
    for code in default.vat_account_codes
}
_TAX_ACCOUNT_TYPES = {TAX_KIND_INPUT: "asset", TAX_KIND_OUTPUT: "liability"}
# SKR03 1401–1406 sind reserviert und z. B. als Forderungskonten zuteilbar:
# Ohne Steuerbegriff in der Bezeichnung zählt die Nummer allein nicht.
_TAX_ACCOUNT_NAME_PARTS = ("steuer", "ust", "vst")


# Steuerkonten für den innergemeinschaftlichen Erwerb ("ig_erwerb") und die
# Steuer nach § 13b UStG als Leistungsempfänger ("reverse_charge") laut
# DATEV-Kontenrahmen 2026 (SKR03 / SKR04). Konten ohne Satz in der Bezeichnung
# (1572, 1578, 1772, 1785 …) führen DATEV-seitig die übrigen Steuersätze.
SPECIAL_TAX_ACCOUNTS: dict[str, tuple[str, str, Decimal | None]] = {
    # Vorsteuer
    "1572": ("ig_erwerb", TAX_KIND_INPUT, None),
    "1574": ("ig_erwerb", TAX_KIND_INPUT, Decimal("19")),
    "1402": ("ig_erwerb", TAX_KIND_INPUT, None),
    "1404": ("ig_erwerb", TAX_KIND_INPUT, Decimal("19")),
    "1577": ("reverse_charge", TAX_KIND_INPUT, Decimal("19")),
    "1578": ("reverse_charge", TAX_KIND_INPUT, None),
    "1407": ("reverse_charge", TAX_KIND_INPUT, Decimal("19")),
    "1408": ("reverse_charge", TAX_KIND_INPUT, None),
    # Umsatzsteuer
    "1772": ("ig_erwerb", TAX_KIND_OUTPUT, None),
    "1774": ("ig_erwerb", TAX_KIND_OUTPUT, Decimal("19")),
    "3802": ("ig_erwerb", TAX_KIND_OUTPUT, None),
    "3804": ("ig_erwerb", TAX_KIND_OUTPUT, Decimal("19")),
    "1785": ("reverse_charge", TAX_KIND_OUTPUT, None),
    "1787": ("reverse_charge", TAX_KIND_OUTPUT, Decimal("19")),
    "3835": ("reverse_charge", TAX_KIND_OUTPUT, None),
    "3837": ("reverse_charge", TAX_KIND_OUTPUT, Decimal("19")),
}


@dataclass(slots=True, frozen=True)
class SpecialTaxAccount:
    """Steuerkonto für ig. Erwerb oder § 13b UStG (Leistungsempfänger)."""

    category: str  # "ig_erwerb" | "reverse_charge"
    kind: str
    rate: Decimal | None


def _is_tax_account(account: Account, kind: str) -> bool:
    name = (account.name or "").casefold()
    return account.account_type == _TAX_ACCOUNT_TYPES[kind] and any(
        part in name for part in _TAX_ACCOUNT_NAME_PARTS
    )


def company_special_tax_accounts(
    *, session: Session, company_id: int
) -> dict[int, SpecialTaxAccount]:
    """Steuerkonten für ig. Erwerb und § 13b UStG (Standardnummern, Kontoart und
    Bezeichnung wie bei ``company_tax_accounts``); auch deaktivierte Konten."""
    result: dict[int, SpecialTaxAccount] = {}
    for account in session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_(SPECIAL_TAX_ACCOUNTS),
        )
    ).scalars():
        category, kind, rate = SPECIAL_TAX_ACCOUNTS[account.code]
        if _is_tax_account(account, kind):
            result[account.id] = SpecialTaxAccount(category=category, kind=kind, rate=rate)
    return result


@dataclass(slots=True, frozen=True)
class TaxAccount:
    """Steuerkonto mit Richtung und Steuersatz (None, wenn Codes mehrerer Sätze darauf zeigen)."""

    kind: str
    rate: Decimal | None


def company_tax_accounts(*, session: Session, company_id: int) -> dict[int, TaxAccount]:
    """Steuerkonten einer Gesellschaft, über die UStVA und DATEV-Export Steuerzeilen erkennen.

    Maßgeblich sind die Steuerkonten der Steuercodes. Daneben zählen die
    Standard-Steuerkonten (``STANDARD_TAX_ACCOUNTS``), auf die kein Steuercode
    zeigt, sofern Kontoart (Vorsteuer aktiv, Umsatzsteuer passiv) und Bezeichnung
    passen – so funktioniert die Steuer auch ohne Steuercodes. Deaktivierte Konten
    zählen mit, damit Altbuchungen in früheren Zeiträumen erkannt bleiben.
    """
    kinds: dict[int, str] = {}
    rates: dict[int, set[Decimal]] = {}
    for vat_account_id, kind, rate in session.execute(
        select(TaxCode.vat_account_id, TaxCode.kind, TaxCode.rate).where(
            TaxCode.company_id == company_id, TaxCode.vat_account_id.is_not(None)
        )
    ):
        # Die Kontoart des Steuerkontos legt die Richtung fest (normalize_tax_kind).
        kinds[vat_account_id] = kind
        if rate is not None and rate > Decimal("0.00"):
            rates.setdefault(vat_account_id, set()).add(Decimal(rate))
    result = {
        account_id: TaxAccount(
            kind=kind,
            rate=next(iter(rates[account_id])) if len(rates.get(account_id, ())) == 1 else None,
        )
        for account_id, kind in kinds.items()
    }

    for account in session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.code.in_(STANDARD_TAX_ACCOUNTS),
        )
    ).scalars():
        if account.id in result:
            continue
        kind, rate = STANDARD_TAX_ACCOUNTS[account.code]
        if _is_tax_account(account, kind):
            result[account.id] = TaxAccount(kind=kind, rate=rate)
    return result


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
