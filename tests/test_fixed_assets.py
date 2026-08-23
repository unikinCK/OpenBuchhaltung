from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.fixed_assets import (
    FixedAssetError,
    FixedAssetInput,
    cancel_fixed_asset,
    create_fixed_asset,
    current_book_value,
    depreciation_schedule,
    dispose_fixed_asset,
    list_fixed_assets,
    post_depreciation,
    record_impairment,
    update_fixed_asset,
)
from domain.models import (
    Account,
    AuditLog,
    Base,
    Company,
    ControllingUnit,
    DepreciationEntry,
    FixedAsset,
    JournalEntryLine,
    Tenant,
    User,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> tuple[Company, Account, Account]:
    tenant = Tenant(name="Anlagen Tenant")
    company = Company(tenant=tenant, name="Anlagen GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    machine = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="0400",
        name="Maschinen",
        account_type="asset",
    )
    afa_expense = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="4830",
        name="Abschreibungen auf Sachanlagen",
        account_type="expense",
    )
    session.add_all([machine, afa_expense])
    session.commit()
    return company, machine, afa_expense


def _linear_asset(session: Session, company: Company) -> FixedAsset:
    return create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-001",
            name="Drehmaschine",
            acquisition_date=date(2026, 1, 1),
            acquisition_cost=Decimal("12000.00"),
            method="linear",
            useful_life_months=60,
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )


def test_create_and_schedule_linear(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    assert asset.id is not None
    rows = depreciation_schedule(asset)
    assert len(rows) == 5
    assert rows[0].depreciation == Decimal("2400.00")


def test_post_depreciation_creates_balanced_entry(session: Session) -> None:
    company, machine, afa_expense = _seed(session)
    asset = _linear_asset(session, company)

    entry = post_depreciation(
        session=session,
        fixed_asset_id=asset.id,
        fiscal_year=2026,
        changed_by="pytest",
    )
    assert entry.amount == Decimal("2400.00")
    assert entry.book_value_after == Decimal("9600.00")
    assert current_book_value(session=session, asset=asset) == Decimal("9600.00")

    lines = session.execute(
        select(JournalEntryLine).where(
            JournalEntryLine.journal_entry_id == entry.journal_entry_id
        )
    ).scalars().all()
    debit = {line.account_id: line.debit_amount for line in lines if line.debit_amount > 0}
    credit = {line.account_id: line.credit_amount for line in lines if line.credit_amount > 0}
    assert debit == {afa_expense.id: Decimal("2400.00")}
    assert credit == {machine.id: Decimal("2400.00")}


def test_depreciation_inherits_asset_controlling_defaults(session: Session) -> None:
    company, machine, afa_expense = _seed(session)
    cost_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="cost_center",
        code="K200",
        name="Produktion",
    )
    profit_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="profit_center",
        code="P200",
        name="Maschinenbau",
    )
    session.add_all([cost_center, profit_center])
    session.commit()
    asset = create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-CO-1",
            name="Kontierte Maschine",
            acquisition_date=date(2026, 1, 1),
            acquisition_cost=Decimal("12000.00"),
            method="linear",
            useful_life_months=60,
            asset_account_id=machine.id,
            depreciation_account_id=afa_expense.id,
            cost_center_id=cost_center.id,
            profit_center_id=profit_center.id,
            changed_by="pytest",
        ),
    )

    depreciation = post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    lines = session.execute(
        select(JournalEntryLine).where(
            JournalEntryLine.journal_entry_id == depreciation.journal_entry_id
        )
    ).scalars().all()
    expense_line = next(line for line in lines if line.account_id == afa_expense.id)
    asset_line = next(line for line in lines if line.account_id == machine.id)
    assert expense_line.cost_center_id == cost_center.id
    assert expense_line.profit_center_id == profit_center.id
    assert asset_line.cost_center_id is None
    assert asset_line.profit_center_id is None


def test_post_depreciation_is_idempotent_per_year(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    with pytest.raises(FixedAssetError, match="bereits eine planmäßige"):
        post_depreciation(
            session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
        )


def test_full_lifecycle_marks_fully_depreciated(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    for year in range(2026, 2031):
        post_depreciation(
            session=session, fixed_asset_id=asset.id, fiscal_year=year, changed_by="pytest"
        )
    session.refresh(asset)
    assert current_book_value(session=session, asset=asset) == Decimal("0.00")
    assert asset.status == "fully_depreciated"


def test_leistungs_afa_requires_units(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-LEI",
            name="Maschine nach Leistung",
            acquisition_date=date(2026, 1, 1),
            acquisition_cost=Decimal("100000.00"),
            method="leistung",
            total_units=Decimal("100000"),
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )
    with pytest.raises(FixedAssetError, match="Jahresleistung"):
        post_depreciation(
            session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
        )
    entry = post_depreciation(
        session=session,
        fixed_asset_id=asset.id,
        fiscal_year=2026,
        changed_by="pytest",
        units=Decimal("25000"),
    )
    assert entry.amount == Decimal("25000.00")


def test_impairment_reduces_book_value(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    record_impairment(
        session=session,
        fixed_asset_id=asset.id,
        fiscal_year=2026,
        amount=Decimal("3000.00"),
        changed_by="pytest",
    )
    assert current_book_value(session=session, asset=asset) == Decimal("9000.00")


def test_dispose_writes_off_residual(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    disposed = dispose_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        disposal_date=date(2027, 6, 30),
        proceeds=Decimal("5000.00"),
        changed_by="pytest",
    )
    assert disposed.status == "disposed"
    assert current_book_value(session=session, asset=asset) == Decimal("0.00")
    kinds = session.execute(
        select(DepreciationEntry.kind).where(DepreciationEntry.fixed_asset_id == asset.id)
    ).scalars().all()
    assert "abgang" in kinds


def test_gwg_asset_schedule(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="GWG-1",
            name="Bürostuhl",
            acquisition_date=date(2026, 3, 1),
            acquisition_cost=Decimal("700.00"),
            method="gwg",
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )
    entry = post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    assert entry.amount == Decimal("700.00")
    assert current_book_value(session=session, asset=asset) == Decimal("0.00")


def test_unknown_account_rejected(session: Session) -> None:
    company, _, _ = _seed(session)
    with pytest.raises(FixedAssetError, match="Anlagekonto"):
        create_fixed_asset(
            session=session,
            payload=FixedAssetInput(
                company_id=company.id,
                asset_number="A-BAD",
                name="Ohne Konto",
                acquisition_date=date(2026, 1, 1),
                acquisition_cost=Decimal("1000.00"),
                method="linear",
                useful_life_months=12,
                asset_account_code="9999",
                depreciation_account_code="4830",
                changed_by="pytest",
            ),
        )


def test_audit_events_recorded(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    actions = session.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "fixed_asset")
    ).scalars().all()
    assert "created" in actions
    assert "depreciation_planmaessig" in actions


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_fixed_assets.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    return app


def test_fixed_assets_ui_create_and_depreciate(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "Anlagen UI", "company_name": "Anlagen UI GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "0400", "name": "Maschinen", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )

    page = client.get("/anlagen?company_id=1")
    assert page.status_code == 200
    assert "Anlagen".encode() in page.data

    create_response = client.post(
        "/anlagen",
        data={
            "company_id": "1",
            "asset_number": "A-UI-1",
            "name": "UI Maschine",
            "acquisition_date": "2026-01-01",
            "acquisition_cost": "12000.00",
            "method": "linear",
            "useful_life_months": "60",
            "asset_account_id": "1",
            "depreciation_account_id": "2",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert b"A-UI-1" in create_response.data
    # Verfahren wird inkl. Nutzungsdauer angezeigt (macht "Linear" erst eindeutig).
    assert "Linear (§ 7 Abs. 1 EStG) · 60 Monate".encode() in create_response.data

    depreciate_response = client.post(
        "/anlagen/1/abschreiben",
        data={"company_id": "1", "fiscal_year": "2026"},
        follow_redirects=True,
    )
    assert depreciate_response.status_code == 200

    with app.extensions["db_session_factory"]() as db_session:
        asset = db_session.get(FixedAsset, 1)
        assert current_book_value(session=db_session, asset=asset) == Decimal("9600.00")


def test_fixed_assets_api_impairment_and_disposal(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Anlagen API", "company_name": "Anlagen API GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "0400", "name": "Maschinen", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    create_response = client.post(
        "/api/v1/fixed-assets",
        json={
            "company_id": 1,
            "asset_number": "A-API-1",
            "name": "API Maschine",
            "acquisition_date": "2026-01-01",
            "acquisition_cost": "12000.00",
            "method": "linear",
            "useful_life_months": 60,
            "asset_account_id": 1,
            "depreciation_account_id": 2,
        },
    )
    assert create_response.status_code == 201
    asset_id = create_response.get_json()["id"]

    impairment = client.post(
        f"/api/v1/fixed-assets/{asset_id}/impairment",
        json={"fiscal_year": 2026, "amount": "1000.00"},
    )
    assert impairment.status_code == 201
    assert impairment.get_json()["kind"] == "ausserplanmaessig"
    assert impairment.get_json()["book_value_after"] == "11000.00"

    disposal = client.post(
        f"/api/v1/fixed-assets/{asset_id}/disposal",
        json={"disposal_date": "2026-12-31", "proceeds": "5000.00"},
    )
    assert disposal.status_code == 200
    assert disposal.get_json()["status"] == "disposed"
    assert disposal.get_json()["book_value"] == "0.00"


def test_update_fixed_asset_corrects_name_and_cost(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)

    updated = update_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        name="Apple MacBook Air 13\" M5",
        acquisition_cost=Decimal("1324.41"),
        changed_by="pytest",
    )
    assert updated.name == "Apple MacBook Air 13\" M5"
    assert updated.acquisition_cost == Decimal("1324.41")
    assert current_book_value(session=session, asset=updated) == Decimal("1324.41")

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "fixed_asset", AuditLog.action == "updated"
        )
    ).scalar_one()
    assert audit.payload["changes"]["acquisition_cost"]["new"] == "1324.41"


def test_update_fixed_asset_cost_blocked_after_depreciation(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )

    # Name bleibt änderbar, AK nicht mehr.
    updated = update_fixed_asset(
        session=session, fixed_asset_id=asset.id, name="Neuer Name", changed_by="pytest"
    )
    assert updated.name == "Neuer Name"
    with pytest.raises(FixedAssetError, match="Abschreibungen gebucht"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            acquisition_cost=Decimal("9999.00"),
            changed_by="pytest",
        )


def test_update_fixed_asset_requires_changes(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    with pytest.raises(FixedAssetError, match="Keine Änderungen"):
        update_fixed_asset(session=session, fixed_asset_id=asset.id, changed_by="pytest")


def test_cancel_fixed_asset_without_booking(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)

    cancelled = cancel_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        reason="Gerät weiterverkauft, gehört nicht ins Anlagevermögen",
        changed_by="pytest",
    )
    assert cancelled.status == "cancelled"
    assert "weiterverkauft" in (cancelled.notes or "")

    # Kein Abgang gebucht: keine Abschreibungszeilen entstanden.
    entries = session.execute(
        select(DepreciationEntry).where(DepreciationEntry.fixed_asset_id == asset.id)
    ).scalars().all()
    assert entries == []

    # Stornierte Anlagen sind für AfA/Abgang gesperrt und fliegen aus der aktiven Liste.
    with pytest.raises(FixedAssetError, match="storniert"):
        post_depreciation(
            session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
        )
    with pytest.raises(FixedAssetError, match="storniert"):
        dispose_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            disposal_date=date(2026, 12, 31),
            changed_by="pytest",
        )
    active = list_fixed_assets(
        session=session, company_id=company.id, include_disposed=False
    )
    assert asset.id not in [a.id for a in active]

    actions = session.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "fixed_asset")
    ).scalars().all()
    assert "cancelled" in actions


def test_cancel_fixed_asset_blocked_after_depreciation(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    with pytest.raises(FixedAssetError, match="Anlagenabgang"):
        cancel_fixed_asset(session=session, fixed_asset_id=asset.id, changed_by="pytest")


def test_fixed_assets_api_update_and_cancel(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Anlagen API 2", "company_name": "Anlagen API 2 GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "0400", "name": "BGA", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    created = client.post(
        "/api/v1/fixed-assets",
        json={
            "company_id": 1,
            "asset_number": "A-API-2",
            "name": "Apple iMac 24 M4",
            "acquisition_date": "2026-03-20",
            "acquisition_cost": "1394.12",
            "method": "linear",
            "useful_life_months": 12,
            "asset_account_id": 1,
            "depreciation_account_id": 2,
        },
    )
    assert created.status_code == 201
    asset_id = created.get_json()["id"]

    updated = client.patch(
        f"/api/v1/fixed-assets/{asset_id}",
        json={"name": "Apple MacBook Air 13 M5", "acquisition_cost": "1324.41"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["name"] == "Apple MacBook Air 13 M5"
    assert updated.get_json()["acquisition_cost"] == "1324.41"

    no_changes = client.patch(f"/api/v1/fixed-assets/{asset_id}", json={})
    assert no_changes.status_code == 422

    cancelled = client.post(
        f"/api/v1/fixed-assets/{asset_id}/cancel",
        json={"reason": "fälschlich als Anlage erfasst"},
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "cancelled"

    again = client.post(f"/api/v1/fixed-assets/{asset_id}/cancel", json={})
    assert again.status_code == 422


def test_fixed_assets_ui_update_and_cancel(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "Anlagen UI 2", "company_name": "UI 2 GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "0400", "name": "BGA", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    client.post(
        "/anlagen",
        data={
            "company_id": "1",
            "asset_number": "A-UI-2",
            "name": "Falsches Gerät",
            "acquisition_date": "2026-03-20",
            "acquisition_cost": "1394.12",
            "method": "linear",
            "useful_life_months": "12",
            "asset_account_id": "1",
            "depreciation_account_id": "2",
        },
    )

    update = client.post(
        "/anlagen/1/bearbeiten",
        data={"company_id": "1", "name": "Richtiges Gerät", "acquisition_cost": "1324.41"},
        follow_redirects=True,
    )
    assert update.status_code == 200
    assert "Richtiges Gerät".encode() in update.data

    cancel = client.post(
        "/anlagen/1/stornieren",
        data={"company_id": "1", "reason": "Testkorrektur"},
        follow_redirects=True,
    )
    assert cancel.status_code == 200
    assert b"storniert" in cancel.data

    with app.extensions["db_session_factory"]() as db_session:
        asset = db_session.get(FixedAsset, 1)
        assert asset.status == "cancelled"
        assert asset.name == "Richtiges Gerät"


def test_digital_method_full_writeoff_in_acquisition_year(session: Session) -> None:
    company, machine, afa_expense = _seed(session)
    asset = create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-DIG-1",
            name="MacBook Air 13 M5",
            acquisition_date=date(2026, 3, 20),
            acquisition_cost=Decimal("1324.41"),
            method="digital",
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )
    rows = depreciation_schedule(asset)
    # Volle AfA im Zugangsjahr trotz Inbetriebnahme im März (BMF v. 22.02.2022).
    assert len(rows) == 1
    assert rows[0].year == 2026
    assert rows[0].depreciation == Decimal("1324.41")
    assert rows[0].book_value_end == Decimal("0.00")
    assert "BMF" in rows[0].note

    entry = post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    assert entry.amount == Decimal("1324.41")
    assert current_book_value(session=session, asset=asset) == Decimal("0.00")
    session.refresh(asset)
    assert asset.status == "fully_depreciated"


def test_update_method_to_digital_before_depreciation(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)  # linear, 60 Monate

    updated = update_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        method="digital",
        useful_life_months=12,
        changed_by="pytest",
    )
    assert updated.method == "digital"
    assert updated.useful_life_months == 12
    rows = depreciation_schedule(updated)
    assert len(rows) == 1
    assert rows[0].depreciation == Decimal("12000.00")

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "fixed_asset", AuditLog.action == "updated"
        )
    ).scalar_one()
    assert audit.payload["changes"]["method"] == {"old": "linear", "new": "digital"}


def test_update_method_to_degressive_with_rate(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)  # linear, 60 Monate

    updated = update_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        method="degressive",
        degressive_rate=Decimal("20"),
        changed_by="pytest",
    )
    assert updated.method == "degressive"
    assert updated.degressive_rate == Decimal("20")
    rows = depreciation_schedule(updated)
    assert rows[0].depreciation == Decimal("2400.00")

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "fixed_asset", AuditLog.action == "updated"
        )
    ).scalar_one()
    assert audit.payload["changes"]["degressive_rate"] == {"old": None, "new": "20"}


def test_update_to_degressive_without_rate_rejected(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    with pytest.raises(FixedAssetError, match="Prozentsatz erforderlich"):
        update_fixed_asset(
            session=session, fixed_asset_id=asset.id, method="degressive", changed_by="pytest"
        )
    with pytest.raises(FixedAssetError, match="Prozentsatz muss größer 0"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            method="degressive",
            degressive_rate=Decimal("-5"),
            changed_by="pytest",
        )


def test_update_total_units_and_in_service_date(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)

    updated = update_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        method="leistung",
        total_units=Decimal("100000"),
        in_service_date=date(2026, 4, 1),
        changed_by="pytest",
    )
    assert updated.method == "leistung"
    assert updated.total_units == Decimal("100000")
    assert updated.in_service_date == date(2026, 4, 1)

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "fixed_asset", AuditLog.action == "updated"
        )
    ).scalar_one()
    assert audit.payload["changes"]["total_units"] == {"old": None, "new": "100000"}
    assert audit.payload["changes"]["in_service_date"] == {
        "old": "2026-01-01",
        "new": "2026-04-01",
    }

    with pytest.raises(FixedAssetError, match="Gesamtleistung muss größer 0"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            total_units=Decimal("0"),
            changed_by="pytest",
        )


def test_update_plan_params_blocked_after_depreciation(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    with pytest.raises(FixedAssetError, match="Prozentsatz"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            degressive_rate=Decimal("20"),
            changed_by="pytest",
        )
    with pytest.raises(FixedAssetError, match="Gesamtleistung"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            total_units=Decimal("100000"),
            changed_by="pytest",
        )
    with pytest.raises(FixedAssetError, match="Inbetriebnahmedatum"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            in_service_date=date(2026, 6, 1),
            changed_by="pytest",
        )


def test_update_method_blocked_after_depreciation(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    post_depreciation(
        session=session, fixed_asset_id=asset.id, fiscal_year=2026, changed_by="pytest"
    )
    with pytest.raises(FixedAssetError, match="Verfahren"):
        update_fixed_asset(
            session=session, fixed_asset_id=asset.id, method="digital", changed_by="pytest"
        )
    with pytest.raises(FixedAssetError, match="Nutzungsdauer"):
        update_fixed_asset(
            session=session, fixed_asset_id=asset.id, useful_life_months=12, changed_by="pytest"
        )


def test_update_to_invalid_method_or_missing_life_rejected(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    with pytest.raises(FixedAssetError, match="Unbekanntes AfA-Verfahren"):
        update_fixed_asset(
            session=session, fixed_asset_id=asset.id, method="turbo", changed_by="pytest"
        )
    with pytest.raises(FixedAssetError, match="Nutzungsdauer"):
        update_fixed_asset(
            session=session,
            fixed_asset_id=asset.id,
            useful_life_months=-3,
            changed_by="pytest",
        )


def test_fixed_assets_api_patch_method(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Anlagen API 3", "company_name": "Anlagen API 3 GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "0400", "name": "BGA", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    created = client.post(
        "/api/v1/fixed-assets",
        json={
            "company_id": 1,
            "asset_number": "A-API-3",
            "name": "iPhone 17 Pro",
            "acquisition_date": "2026-07-03",
            "acquisition_cost": "952.88",
            "method": "linear",
            "useful_life_months": 60,
            "asset_account_id": 1,
            "depreciation_account_id": 2,
        },
    )
    assert created.status_code == 201
    asset_id = created.get_json()["id"]

    patched = client.patch(
        f"/api/v1/fixed-assets/{asset_id}",
        json={"method": "digital", "useful_life_months": 12},
    )
    assert patched.status_code == 200
    body = patched.get_json()
    assert body["method"] == "digital"
    assert body["useful_life_months"] == 12

    schedule = client.get(f"/api/v1/fixed-assets/{asset_id}/schedule")
    rows = schedule.get_json()["schedule"]
    assert len(rows) == 1
    assert rows[0]["depreciation"] == "952.88"


def test_fixed_assets_api_patch_to_degressive(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Anlagen API 4", "company_name": "Anlagen API 4 GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "0400", "name": "BGA", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    created = client.post(
        "/api/v1/fixed-assets",
        json={
            "company_id": 1,
            "asset_number": "A-2026-005",
            "name": "Schreibtisch",
            "acquisition_date": "2026-01-01",
            "acquisition_cost": "1300.00",
            "method": "linear",
            "useful_life_months": 156,
            "asset_account_id": 1,
            "depreciation_account_id": 2,
        },
    )
    assert created.status_code == 201
    asset_id = created.get_json()["id"]

    # Ohne Prozentsatz bleibt die Umstellung wie bisher abgelehnt.
    rejected = client.patch(
        f"/api/v1/fixed-assets/{asset_id}", json={"method": "degressive"}
    )
    assert rejected.status_code == 422
    assert "Prozentsatz" in rejected.get_json()["error"]

    patched = client.patch(
        f"/api/v1/fixed-assets/{asset_id}",
        json={
            "method": "degressive",
            # Spaltenpräzision ist Numeric(5,2), daher auf 2 Nachkommastellen.
            "degressive_rate": "23.08",
            "in_service_date": "2026-02-01",
        },
    )
    assert patched.status_code == 200
    body = patched.get_json()
    assert body["method"] == "degressive"
    assert body["degressive_rate"] == "23.08"
    assert body["in_service_date"] == "2026-02-01"

    total_units = client.patch(
        f"/api/v1/fixed-assets/{asset_id}",
        json={"method": "leistung", "total_units": "100000"},
    )
    assert total_units.status_code == 200
    assert total_units.get_json()["total_units"] == "100000.00"

    invalid = client.patch(
        f"/api/v1/fixed-assets/{asset_id}", json={"degressive_rate": "abc"}
    )
    assert invalid.status_code == 400


def test_notes_length_is_validated(session: Session) -> None:
    company, _, _ = _seed(session)
    asset = _linear_asset(session, company)
    too_long = "x" * 300

    # Update und Neuanlage: klare Fehlermeldung statt DB-Fehler (Postgres: String(255)).
    with pytest.raises(FixedAssetError, match="255 Zeichen"):
        update_fixed_asset(
            session=session, fixed_asset_id=asset.id, notes=too_long, changed_by="pytest"
        )
    with pytest.raises(FixedAssetError, match="255 Zeichen"):
        create_fixed_asset(
            session=session,
            payload=FixedAssetInput(
                company_id=company.id,
                asset_number="A-notes",
                name="Notiztest",
                acquisition_date=date(2026, 1, 1),
                acquisition_cost=Decimal("100.00"),
                method="gwg",
                asset_account_code="0400",
                depreciation_account_code="4830",
                notes=too_long,
                changed_by="pytest",
            ),
        )

    # Storno: angehängter Grund wird auf die Spaltenlänge gekappt statt zu scheitern.
    update_fixed_asset(
        session=session, fixed_asset_id=asset.id, notes="x" * 250, changed_by="pytest"
    )
    cancelled = cancel_fixed_asset(
        session=session,
        fixed_asset_id=asset.id,
        reason="sehr langer Stornogrund " * 5,
        changed_by="pytest",
    )
    assert cancelled.notes is not None and len(cancelled.notes) <= 255


def test_degressive_rate_keeps_four_decimal_places(session: Session) -> None:
    company, _, _ = _seed(session)
    # 3/13 × 100 = 23,0769 %: mit Numeric(5,2) würde die DB auf 23,08 runden.
    asset = create_fixed_asset(
        session=session,
        payload=FixedAssetInput(
            company_id=company.id,
            asset_number="A-degressiv",
            name="Produktionsanlage",
            acquisition_date=date(2026, 1, 1),
            acquisition_cost=Decimal("130000.00"),
            method="degressive",
            useful_life_months=156,
            degressive_rate=Decimal("23.0769"),
            asset_account_code="0400",
            depreciation_account_code="4830",
            changed_by="pytest",
        ),
    )

    session.expire(asset)
    reloaded = session.get(FixedAsset, asset.id)
    assert reloaded is not None
    assert reloaded.degressive_rate == Decimal("23.0769")

    rows = depreciation_schedule(reloaded)
    assert rows[0].depreciation == Decimal("29999.97")  # 130000 × 23,0769 %


def test_fixed_assets_api_degressive_rate_roundtrip(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Anlagen API 4", "company_name": "Anlagen API 4 GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "0400", "name": "Maschinen", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "4830",
            "name": "Abschreibungen",
            "account_type": "expense",
        },
    )
    created = client.post(
        "/api/v1/fixed-assets",
        json={
            "company_id": 1,
            "asset_number": "A-API-4",
            "name": "Produktionsanlage",
            "acquisition_date": "2026-01-01",
            "acquisition_cost": "130000.00",
            "method": "degressive",
            "useful_life_months": 156,
            "degressive_rate": "23.0769",
            "asset_account_id": 1,
            "depreciation_account_id": 2,
        },
    )
    assert created.status_code == 201
    assert created.get_json()["degressive_rate"] == "23.0769"

    listed = client.get("/api/v1/fixed-assets?company_id=1")
    assert listed.get_json()["assets"][0]["degressive_rate"] == "23.0769"
