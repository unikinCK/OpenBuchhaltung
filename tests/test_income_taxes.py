from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.services.income_taxes import (
    compute_income_tax_return,
    list_income_tax_returns,
    save_income_tax_return,
)
from domain.models import Base, User
from tests.test_vat_returns import _create_test_app, _seed, _seed_bookings


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Session(engine)


def test_compute_corporate_income_tax_return() -> None:
    with _session() as session:
        company = _seed(session)
        _seed_bookings(session, company)

        result = compute_income_tax_return(
            session=session,
            company_id=company.id,
            year=2026,
            tax_type="corporate_income",
            additions=[
                {
                    "code": "NDA",
                    "label": "Nicht abziehbare Aufwendungen",
                    "amount": "99.50",
                }
            ],
        )

    rows = {row["code"]: Decimal(row["amount"]) for row in result["calculation"]["rows"]}
    assert result["basis"]["net_income"] == "900.50"
    assert result["basis"]["taxable_income"] == "1000.00"
    assert rows["corporate_tax"] == Decimal("150.00")
    assert rows["solidarity_surcharge"] == Decimal("8.25")
    assert rows["payable"] == Decimal("158.25")


def test_save_trade_tax_prepayment_snapshot() -> None:
    with _session() as session:
        company = _seed(session)
        _seed_bookings(session, company)

        item = save_income_tax_return(
            session=session,
            company_id=company.id,
            year=2026,
            tax_type="trade_tax",
            declaration_type="prepayment_adjustment",
            additions=[{"code": "GEW8", "label": "Hinzurechnung GewStG § 8", "amount": "99.50"}],
            municipality_multiplier="400",
            prepayments="40.00",
            changed_by="pytest",
        )

        listed = list_income_tax_returns(session=session, company_id=company.id)

    rows = {row["code"]: Decimal(row["amount"]) for row in item.calculation["calculation"]["rows"]}
    assert item.tax_type == "trade_tax"
    assert item.declaration_type == "prepayment_adjustment"
    assert rows["tax_base_amount"] == Decimal("35.00")
    assert rows["trade_tax"] == Decimal("140.00")
    assert rows["payable"] == Decimal("100.00")
    assert [entry.id for entry in listed] == [item.id]


def test_income_tax_api_preview_create_and_list(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company = _seed(session)
        _seed_bookings(session, company)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        company_id = company.id

    client = app.test_client()
    preview = client.post(
        "/api/v1/income-tax-returns/preview",
        json={
            "company_id": company_id,
            "year": 2026,
            "tax_type": "corporate_income",
            "additions": [
                {"code": "NDA", "label": "Nicht abziehbare Aufwendungen", "amount": "99.50"}
            ],
        },
    )
    assert preview.status_code == 200
    assert preview.get_json()["calculation"]["payable"] == "158.25"

    created = client.post(
        "/api/v1/income-tax-returns",
        json={
            "company_id": company_id,
            "year": 2026,
            "tax_type": "trade_tax",
            "declaration_type": "declaration",
            "additions": [{"code": "GEW8", "label": "Hinzurechnung", "amount": "99.50"}],
            "municipality_multiplier": "400",
        },
    )
    assert created.status_code == 201
    created_payload = created.get_json()
    assert created_payload["tax_type"] == "trade_tax"
    assert created_payload["calculation"]["calculation"]["tax_total"] == "140.00"

    listed = client.get(
        "/api/v1/income-tax-returns", query_string={"company_id": company_id}
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()["income_tax_returns"]] == [
        created_payload["id"]
    ]
