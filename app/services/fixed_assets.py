"""Anlagenbuchhaltung: Anlagegüter anlegen, Abschreibungspläne rechnen und
planmäßige/außerplanmäßige AfA sowie Abgänge verbuchen.

Die Abschreibung wird direkt gebucht (Direktabschreibung):
``Soll Abschreibungsaufwand  an  Haben Anlagekonto``. Der Buchwert eines
Anlageguts ergibt sich aus den Anschaffungskosten abzüglich aller verbuchten
Abschreibungszeilen (planmäßig, außerplanmäßig, Abgang).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.controlling import ControllingError, validate_controlling_assignment
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, Company, DepreciationEntry, FixedAsset
from domain.services import depreciation as afa

CENT = Decimal("0.01")


class FixedAssetError(ValueError):
    """Ungültige Anlagen-/Abschreibungsaktion."""


@dataclass(slots=True)
class FixedAssetInput:
    company_id: int
    asset_number: str
    name: str
    acquisition_date: date
    acquisition_cost: Decimal
    method: str
    changed_by: str = "system"
    in_service_date: date | None = None  # Standard: Anschaffungsdatum
    useful_life_months: int | None = None
    degressive_rate: Decimal | None = None
    total_units: Decimal | None = None
    residual_value: Decimal = Decimal("0.00")
    keep_memo_value: bool = False
    notes: str | None = None
    # Konten per interner ID ODER Kontonummer.
    asset_account_id: int | None = None
    asset_account_code: str | None = None
    depreciation_account_id: int | None = None
    depreciation_account_code: str | None = None
    cost_center_id: int | None = None
    profit_center_id: int | None = None


def _resolve_account(
    *, session: Session, company: Company, account_id: int | None, code: str | None, label: str
) -> Account:
    account: Account | None = None
    if account_id is not None:
        account = session.get(Account, account_id)
    elif code and code.strip():
        account = session.execute(
            select(Account).where(
                Account.company_id == company.id, Account.code == code.strip()
            )
        ).scalar_one_or_none()
    if account is None or account.company_id != company.id:
        raise FixedAssetError(f"{label} nicht gefunden.")
    if not account.is_active:
        raise FixedAssetError(f"{label} ist inaktiv.")
    return account


def _asset_params(
    asset: FixedAsset, units_per_year: dict[int, Decimal] | None = None
) -> afa.AssetParams:
    return afa.AssetParams(
        method=asset.method,
        acquisition_cost=asset.acquisition_cost,
        in_service_date=asset.in_service_date,
        useful_life_months=asset.useful_life_months,
        degressive_rate=asset.degressive_rate,
        residual_value=asset.residual_value,
        keep_memo_value=asset.keep_memo_value,
        total_units=asset.total_units,
        units_per_year=units_per_year or {},
    )


def create_fixed_asset(*, session: Session, payload: FixedAssetInput) -> FixedAsset:
    company = session.get(Company, payload.company_id)
    if company is None:
        raise FixedAssetError("Gesellschaft nicht gefunden.")

    method = payload.method.strip()
    if method not in afa.METHODS:
        raise FixedAssetError(
            "Unbekanntes AfA-Verfahren. Erlaubt: " + ", ".join(sorted(afa.METHODS))
        )

    asset_number = payload.asset_number.strip()
    name = payload.name.strip()
    if not asset_number or not name:
        raise FixedAssetError("Inventarnummer und Bezeichnung sind Pflichtfelder.")
    if payload.acquisition_cost <= Decimal("0.00"):
        raise FixedAssetError("Anschaffungskosten müssen größer 0 sein.")

    asset_account = _resolve_account(
        session=session,
        company=company,
        account_id=payload.asset_account_id,
        code=payload.asset_account_code,
        label="Anlagekonto",
    )
    depreciation_account = _resolve_account(
        session=session,
        company=company,
        account_id=payload.depreciation_account_id,
        code=payload.depreciation_account_code,
        label="Abschreibungskonto",
    )

    in_service = payload.in_service_date or payload.acquisition_date
    assignment_date = max(in_service, date.today())
    try:
        validate_controlling_assignment(
            session=session,
            company_id=company.id,
            entry_date=assignment_date,
            unit_id=payload.cost_center_id,
            expected_type="cost_center",
        )
        validate_controlling_assignment(
            session=session,
            company_id=company.id,
            entry_date=assignment_date,
            unit_id=payload.profit_center_id,
            expected_type="profit_center",
        )
    except ControllingError as exc:
        raise FixedAssetError(str(exc)) from exc
    asset = FixedAsset(
        tenant_id=company.tenant_id,
        company_id=company.id,
        asset_number=asset_number,
        name=name,
        acquisition_date=payload.acquisition_date,
        in_service_date=in_service,
        acquisition_cost=payload.acquisition_cost.quantize(CENT),
        method=method,
        useful_life_months=payload.useful_life_months,
        degressive_rate=payload.degressive_rate,
        total_units=payload.total_units,
        residual_value=(payload.residual_value or Decimal("0.00")).quantize(CENT),
        keep_memo_value=payload.keep_memo_value,
        asset_account_id=asset_account.id,
        depreciation_account_id=depreciation_account.id,
        cost_center_id=payload.cost_center_id,
        profit_center_id=payload.profit_center_id,
        status="active",
        notes=(payload.notes or "").strip() or None,
    )

    # Parameter des Verfahrens früh validieren (z. B. fehlende Nutzungsdauer),
    # außer bei rein nutzungsabhängiger/manueller Buchung.
    if method not in {afa.LEISTUNG, afa.MANUELL}:
        afa.compute_schedule(_asset_params(asset))

    session.add(asset)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        action="created",
        changed_by=payload.changed_by,
        payload={
            "asset_number": asset.asset_number,
            "name": asset.name,
            "method": asset.method,
            "acquisition_cost": str(asset.acquisition_cost),
            "cost_center_id": asset.cost_center_id,
            "profit_center_id": asset.profit_center_id,
        },
    )
    session.commit()
    session.refresh(asset)
    return asset


def list_fixed_assets(
    *, session: Session, company_id: int, include_disposed: bool = True
) -> list[FixedAsset]:
    stmt = (
        select(FixedAsset)
        .options(
            selectinload(FixedAsset.asset_account),
            selectinload(FixedAsset.depreciation_account),
            selectinload(FixedAsset.depreciation_entries),
        )
        .where(FixedAsset.company_id == company_id)
        .order_by(FixedAsset.asset_number, FixedAsset.id)
    )
    if not include_disposed:
        stmt = stmt.where(FixedAsset.status != "disposed")
    return session.execute(stmt).scalars().all()


def posted_depreciation_total(*, session: Session, fixed_asset_id: int) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(DepreciationEntry.amount), 0)).where(
            DepreciationEntry.fixed_asset_id == fixed_asset_id
        )
    )
    return Decimal(total or 0).quantize(CENT)


def current_book_value(*, session: Session, asset: FixedAsset) -> Decimal:
    return (
        asset.acquisition_cost - posted_depreciation_total(session=session, fixed_asset_id=asset.id)
    ).quantize(CENT)


def depreciation_schedule(
    asset: FixedAsset, units_per_year: dict[int, Decimal] | None = None
) -> list[afa.ScheduleRow]:
    return afa.compute_schedule(_asset_params(asset, units_per_year))


def _planned_amount_for_year(
    asset: FixedAsset, fiscal_year: int, units: Decimal | None
) -> tuple[Decimal, str]:
    """Planmäßiger AfA-Betrag eines Jahres samt Notiz."""
    if asset.method == afa.LEISTUNG:
        if units is None or units <= Decimal("0.00"):
            raise FixedAssetError("Für die Leistungs-AfA ist die Jahresleistung anzugeben.")
        params = _asset_params(asset, {fiscal_year: units})
        rows = afa.compute_schedule(params)
        if not rows:
            return Decimal("0.00"), ""
        return rows[0].depreciation, rows[0].note

    for row in afa.compute_schedule(_asset_params(asset)):
        if row.year == fiscal_year:
            return row.depreciation, row.note
    return Decimal("0.00"), ""


def _floor(asset: FixedAsset) -> Decimal:
    return afa.floor_value(_asset_params(asset))


def _book_depreciation(
    *,
    session: Session,
    asset: FixedAsset,
    fiscal_year: int,
    amount: Decimal,
    kind: str,
    depreciation_date: date,
    note: str | None,
    changed_by: str,
) -> DepreciationEntry:
    book_value_before = current_book_value(session=session, asset=asset)
    book_value_after = (book_value_before - amount).quantize(CENT)

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=asset.company_id,
            entry_date=depreciation_date,
            description=f"AfA {asset.asset_number} {asset.name} ({kind}) {fiscal_year}",
            status="posted",
            changed_by=changed_by,
            post_to_closing_period=True,
            lines=[
                JournalLineInput(
                    account_id=asset.depreciation_account_id,
                    debit_amount=amount,
                    credit_amount=Decimal("0.00"),
                    description=f"Abschreibung {asset.asset_number}",
                    cost_center_id=asset.cost_center_id,
                    profit_center_id=asset.profit_center_id,
                ),
                JournalLineInput(
                    account_id=asset.asset_account_id,
                    debit_amount=Decimal("0.00"),
                    credit_amount=amount,
                    description=f"Wertminderung {asset.asset_number}",
                ),
            ],
        ),
    )

    depreciation_entry = DepreciationEntry(
        tenant_id=asset.tenant_id,
        company_id=asset.company_id,
        fixed_asset_id=asset.id,
        journal_entry_id=entry.id,
        fiscal_year=fiscal_year,
        depreciation_date=depreciation_date,
        kind=kind,
        amount=amount,
        book_value_before=book_value_before,
        book_value_after=book_value_after,
        note=note,
    )
    session.add(depreciation_entry)

    if book_value_after <= _floor(asset) and asset.status == "active":
        asset.status = "fully_depreciated"

    log_audit_event(
        session=session,
        tenant_id=asset.tenant_id,
        company_id=asset.company_id,
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        action=f"depreciation_{kind}",
        changed_by=changed_by,
        payload={
            "fiscal_year": fiscal_year,
            "amount": str(amount),
            "book_value_after": str(book_value_after),
            "journal_entry_id": entry.id,
            "cost_center_id": asset.cost_center_id,
            "profit_center_id": asset.profit_center_id,
        },
    )
    session.commit()
    session.refresh(depreciation_entry)
    return depreciation_entry


def _guard_active(asset: FixedAsset) -> None:
    if asset.status == "disposed":
        raise FixedAssetError("Das Anlagegut ist bereits abgegangen.")


def _existing_kind(session: Session, asset_id: int, fiscal_year: int, kind: str) -> bool:
    return (
        session.execute(
            select(DepreciationEntry.id).where(
                DepreciationEntry.fixed_asset_id == asset_id,
                DepreciationEntry.fiscal_year == fiscal_year,
                DepreciationEntry.kind == kind,
            )
        ).first()
        is not None
    )


def post_depreciation(
    *,
    session: Session,
    fixed_asset_id: int,
    fiscal_year: int,
    changed_by: str,
    units: Decimal | None = None,
    depreciation_date: date | None = None,
) -> DepreciationEntry:
    """Verbucht die planmäßige AfA eines Wirtschaftsjahres."""
    asset = session.get(FixedAsset, fixed_asset_id)
    if asset is None:
        raise FixedAssetError("Anlagegut nicht gefunden.")
    _guard_active(asset)
    if _existing_kind(session, asset.id, fiscal_year, "planmaessig"):
        raise FixedAssetError(
            f"Für {fiscal_year} wurde bereits eine planmäßige Abschreibung gebucht."
        )

    amount, note = _planned_amount_for_year(asset, fiscal_year, units)
    amount = amount.quantize(CENT)
    if amount <= Decimal("0.00"):
        raise FixedAssetError(
            f"Für {fiscal_year} ist keine planmäßige Abschreibung vorgesehen."
        )

    book_value = current_book_value(session=session, asset=asset)
    max_amount = (book_value - _floor(asset)).quantize(CENT)
    if max_amount <= Decimal("0.00"):
        raise FixedAssetError("Das Anlagegut ist bereits vollständig abgeschrieben.")
    amount = min(amount, max_amount)

    return _book_depreciation(
        session=session,
        asset=asset,
        fiscal_year=fiscal_year,
        amount=amount,
        kind="planmaessig",
        depreciation_date=depreciation_date or date(fiscal_year, 12, 31),
        note=note or None,
        changed_by=changed_by,
    )


def record_impairment(
    *,
    session: Session,
    fixed_asset_id: int,
    fiscal_year: int,
    amount: Decimal,
    changed_by: str,
    depreciation_date: date | None = None,
    note: str | None = None,
) -> DepreciationEntry:
    """Außerplanmäßige Abschreibung / AfaA (§ 253 Abs. 3 HGB, § 7 Abs. 1 S. 7 EStG)."""
    asset = session.get(FixedAsset, fixed_asset_id)
    if asset is None:
        raise FixedAssetError("Anlagegut nicht gefunden.")
    _guard_active(asset)
    if _existing_kind(session, asset.id, fiscal_year, "ausserplanmaessig"):
        raise FixedAssetError(
            f"Für {fiscal_year} wurde bereits eine außerplanmäßige Abschreibung gebucht."
        )

    amount = amount.quantize(CENT)
    if amount <= Decimal("0.00"):
        raise FixedAssetError("Der Abschreibungsbetrag muss größer 0 sein.")
    book_value = current_book_value(session=session, asset=asset)
    if amount > book_value:
        raise FixedAssetError("Der Betrag übersteigt den aktuellen Buchwert.")

    return _book_depreciation(
        session=session,
        asset=asset,
        fiscal_year=fiscal_year,
        amount=amount,
        kind="ausserplanmaessig",
        depreciation_date=depreciation_date or date(fiscal_year, 12, 31),
        note=note or "außerplanmäßige Abschreibung",
        changed_by=changed_by,
    )


def dispose_fixed_asset(
    *,
    session: Session,
    fixed_asset_id: int,
    disposal_date: date,
    changed_by: str,
    proceeds: Decimal | None = None,
    fiscal_year: int | None = None,
) -> FixedAsset:
    """Bucht den Restbuchwert aus (Anlagenabgang) und markiert das Gut als abgegangen.

    Der etwaige Veräußerungserlös wird am Anlagegut vermerkt; die Ertragsbuchung
    (Erlöse aus Anlagenabgang / Buchgewinn oder -verlust) erfolgt separat als
    reguläre Buchung.
    """
    asset = session.get(FixedAsset, fixed_asset_id)
    if asset is None:
        raise FixedAssetError("Anlagegut nicht gefunden.")
    _guard_active(asset)

    year = fiscal_year or disposal_date.year
    book_value = current_book_value(session=session, asset=asset)
    if book_value > Decimal("0.00") and not _existing_kind(session, asset.id, year, "abgang"):
        _book_depreciation(
            session=session,
            asset=asset,
            fiscal_year=year,
            amount=book_value,
            kind="abgang",
            depreciation_date=disposal_date,
            note="Restbuchwert Anlagenabgang",
            changed_by=changed_by,
        )
        session.refresh(asset)

    asset.status = "disposed"
    asset.disposal_date = disposal_date
    asset.disposal_proceeds = proceeds.quantize(CENT) if proceeds is not None else None

    log_audit_event(
        session=session,
        tenant_id=asset.tenant_id,
        company_id=asset.company_id,
        entity_type="fixed_asset",
        entity_id=str(asset.id),
        action="disposed",
        changed_by=changed_by,
        payload={
            "disposal_date": disposal_date.isoformat(),
            "proceeds": str(asset.disposal_proceeds) if asset.disposal_proceeds else None,
            "residual_book_value": str(book_value),
        },
    )
    session.commit()
    session.refresh(asset)
    return asset
