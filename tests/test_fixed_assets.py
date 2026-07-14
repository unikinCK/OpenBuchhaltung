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
    create_fixed_asset,
    current_book_value,
    depreciation_schedule,
    dispose_fixed_asset,
    post_depreciation,
    record_impairment,
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
