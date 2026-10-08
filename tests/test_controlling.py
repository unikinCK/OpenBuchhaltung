from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

from sqlalchemy import select

from app import create_app
from app.auth import hash_password
from app.services.compliance_integrity import calculate_journal_entry_content_hash
from domain.models import JournalEntry, JournalEntryLine, User


def _app_and_client(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'controlling.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="controller",
                password_hash=hash_password("secret"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()
    client = app.test_client()
    response = client.post(
        "/auth/login", data={"username": "controller", "password": "secret"}
    )
    assert response.status_code == 302
    return app, client


def _seed_company_and_accounts(client) -> int:
    tenant = client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Controlling", "company_name": "Controlling GmbH"},
    )
    assert tenant.status_code == 201
    company_id = tenant.get_json()["company"]["id"]
    for code, name, account_type in (
        ("1200", "Bank", "asset"),
        ("8400", "Erlöse", "revenue"),
        ("4400", "Fremdleistungen", "expense"),
    ):
        response = client.post(
            "/api/v1/accounts",
            json={
                "company_id": company_id,
                "code": code,
                "name": name,
                "account_type": account_type,
            },
        )
        assert response.status_code == 201
    return company_id


def _create_unit(client, company_id: int, unit_type: str, code: str, name: str):
    response = client.post(
        "/api/v1/controlling-units",
        json={
            "company_id": company_id,
            "unit_type": unit_type,
            "code": code,
            "name": name,
            "valid_from": "2026-01-01",
        },
    )
    assert response.status_code == 201
    return response.get_json()


def test_controlling_master_data_history_booking_report_and_export(tmp_path: Path) -> None:
    app, client = _app_and_client(tmp_path)
    company_id = _seed_company_and_accounts(client)
    cost_center = _create_unit(client, company_id, "cost_center", "K100", "Vertrieb")
    profit_center = _create_unit(client, company_id, "profit_center", "P100", "Produkte")

    duplicate = client.post(
        "/api/v1/controlling-units",
        json={
            "company_id": company_id,
            "unit_type": "cost_center",
            "code": "K100",
            "name": "Doppelt",
        },
    )
    assert duplicate.status_code == 409

    updated = client.patch(
        f"/api/v1/controlling-units/{cost_center['id']}",
        json={"name": "Vertrieb Inland", "valid_to": "2027-12-31"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["changed"] is True
    immutable = client.patch(
        f"/api/v1/controlling-units/{cost_center['id']}", json={"code": "K200"}
    )
    assert immutable.status_code == 400
    history = client.get(
        f"/api/v1/controlling-units/{cost_center['id']}/history"
    ).get_json()["entries"]
    assert [entry["action"] for entry in history] == ["updated", "created"]
    assert history[0]["payload"]["before"]["name"] == "Vertrieb"
    assert history[0]["payload"]["after"]["name"] == "Vertrieb Inland"

    accounts = client.get(
        f"/api/v1/accounts?company_id={company_id}&include_inactive=true"
    ).get_json()["accounts"]
    account_ids = {account["code"]: account["id"] for account in accounts}
    booking = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-14",
            "description": "Umsatz Vertrieb",
            "lines": [
                {"account_id": account_ids["1200"], "debit_amount": "250.00"},
                {
                    "account_id": account_ids["8400"],
                    "credit_amount": "250.00",
                    "cost_center_id": cost_center["id"],
                    "profit_center_id": profit_center["id"],
                },
            ],
        },
    )
    assert booking.status_code == 201
    entry_id = booking.get_json()["id"]

    listed = client.get(f"/api/v1/journal-entries?company_id={company_id}").get_json()
    revenue_line = listed["entries"][0]["lines"][1]
    assert revenue_line["cost_center_code"] == "K100"
    assert revenue_line["profit_center_code"] == "P100"

    cost_report = client.get(
        f"/api/v1/controlling-report?company_id={company_id}&unit_type=cost_center"
    ).get_json()
    assert cost_report["units"][0]["total_revenue"] == "250.00"
    assert cost_report["units"][0]["net_income"] == "250.00"
    profit_report = client.get(
        f"/api/v1/controlling-report?company_id={company_id}&unit_type=profit_center"
    ).get_json()
    assert profit_report["units"][0]["total_revenue"] == "250.00"

    finalized = client.post(f"/api/v1/journal-entries/{entry_id}/finalize")
    assert finalized.status_code == 200
    with app.extensions["db_session_factory"]() as session:
        entry = session.get(JournalEntry, entry_id)
        assert entry.content_hash_version == 2
        stored_hash = entry.content_hash
        original_dimensions = [
            (line.cost_center_id, line.profit_center_id)
            for line in sorted(entry.lines, key=lambda row: row.line_number)
        ]
        entry.lines[1].cost_center_id = None
        assert calculate_journal_entry_content_hash(entry) != stored_hash
        session.rollback()

    reversal = client.post(
        f"/api/v1/journal-entries/{entry_id}/reverse",
        json={"reversal_date": "2026-07-15"},
    )
    assert reversal.status_code == 201
    reversal_id = reversal.get_json()["id"]
    with app.extensions["db_session_factory"]() as session:
        reversal_dimensions = session.execute(
            select(JournalEntryLine.cost_center_id, JournalEntryLine.profit_center_id)
            .where(JournalEntryLine.journal_entry_id == reversal_id)
            .order_by(JournalEntryLine.line_number)
        ).all()
    assert [tuple(row) for row in reversal_dimensions] == original_dimensions

    journal_csv = client.get(f"/api/v1/exports/journal.csv?company_id={company_id}")
    assert journal_csv.status_code == 200
    assert b"cost_center_code,profit_center_code" in journal_csv.data
    assert b"K100,P100" in journal_csv.data
    controlling_csv = client.get(
        f"/api/v1/exports/controlling.csv?company_id={company_id}&unit_type=cost_center"
    )
    assert controlling_csv.status_code == 200
    assert b"K100,Vertrieb Inland,8400" in controlling_csv.data

    package = client.get(
        f"/api/v1/exports/audit-package.zip?company_id={company_id}&include_documents=false"
    )
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        units = json.loads(archive.read("data/controlling_units.json"))
        unit_history = json.loads(archive.read("data/controlling_unit_history.json"))
        journal_lines = json.loads(archive.read("data/journal_entry_lines.json"))
    assert {unit["code"] for unit in units} == {"K100", "P100"}
    assert len(unit_history) == 3
    assert any(line["cost_center_id"] == cost_center["id"] for line in journal_lines)


def test_controlling_assignment_validity_type_and_ui(tmp_path: Path) -> None:
    _, client = _app_and_client(tmp_path)
    company_id = _seed_company_and_accounts(client)
    cost_center = _create_unit(client, company_id, "cost_center", "K100", "Vertrieb")
    profit_center = _create_unit(client, company_id, "profit_center", "P100", "Produkte")
    accounts = client.get(f"/api/v1/accounts?company_id={company_id}").get_json()["accounts"]
    account_ids = {account["code"]: account["id"] for account in accounts}

    wrong_type = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-14",
            "description": "Falscher Typ",
            "lines": [
                {"account_id": account_ids["1200"], "debit_amount": "10.00"},
                {
                    "account_id": account_ids["8400"],
                    "credit_amount": "10.00",
                    "cost_center_id": profit_center["id"],
                },
            ],
        },
    )
    assert wrong_type.status_code == 422
    assert "Kostenstelle" in wrong_type.get_json()["details"][0]["message"]

    client.patch(
        f"/api/v1/controlling-units/{cost_center['id']}", json={"is_active": False}
    )
    inactive = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-14",
            "description": "Inaktive Kostenstelle",
            "lines": [
                {"account_id": account_ids["1200"], "debit_amount": "10.00"},
                {
                    "account_id": account_ids["8400"],
                    "credit_amount": "10.00",
                    "cost_center_id": cost_center["id"],
                },
            ],
        },
    )
    assert inactive.status_code == 422
    assert "inaktiv" in inactive.get_json()["details"][0]["message"]

    page = client.get(f"/controlling?company_id={company_id}")
    assert page.status_code == 200
    assert "Kostenstellenrechnung".encode() in page.data
    assert b"K100" in page.data
    journal_page = client.get(f"/buchungen?company_id={company_id}")
    assert journal_page.status_code == 200
    assert "Profitcenter".encode() in journal_page.data


def test_controlling_report_counts_skr03_income_accounts_as_revenue(tmp_path: Path) -> None:
    """SKR-Importe legen Erlöskonten als ``income`` an — sie zählen als Erlös."""
    _, client = _app_and_client(tmp_path)
    tenant = client.post(
        "/api/v1/tenants",
        json={"tenant_name": "SKR03", "company_name": "SKR03 GmbH"},
    )
    assert tenant.status_code == 201
    company_id = tenant.get_json()["company"]["id"]
    imported = client.post(
        "/api/v1/account-chart/import",
        json={"company_id": company_id, "chart": "skr03"},
    )
    assert imported.status_code == 201
    accounts = client.get(f"/api/v1/accounts?company_id={company_id}").get_json()["accounts"]
    account_ids = {account["code"]: account["id"] for account in accounts}
    account_types = {account["code"]: account["account_type"] for account in accounts}
    assert account_types["8400"] == account_types["2600"] == "income"

    cost_center = _create_unit(client, company_id, "cost_center", "K100", "Vertrieb")
    profit_center = _create_unit(client, company_id, "profit_center", "P100", "Produkte")
    dimensions = {
        "cost_center_id": cost_center["id"],
        "profit_center_id": profit_center["id"],
    }
    for description, lines in (
        (
            "Umsatz Vertrieb",
            [
                {"account_id": account_ids["1200"], "debit_amount": "1000.00"},
                {"account_id": account_ids["8400"], "credit_amount": "1000.00", **dimensions},
            ],
        ),
        (
            "Versicherung Vertrieb",
            [
                {"account_id": account_ids["4400"], "debit_amount": "300.00", **dimensions},
                {"account_id": account_ids["1200"], "credit_amount": "300.00"},
            ],
        ),
        (
            "Sonstiger Ertrag ohne Kontierung",
            [
                {"account_id": account_ids["1200"], "debit_amount": "50.00"},
                {"account_id": account_ids["2600"], "credit_amount": "50.00"},
            ],
        ),
    ):
        booking = client.post(
            "/api/v1/journal-entries",
            json={
                "company_id": company_id,
                "entry_date": "2026-07-14",
                "description": description,
                "lines": lines,
            },
        )
        assert booking.status_code == 201

    for unit_type in ("cost_center", "profit_center"):
        report = client.get(
            f"/api/v1/controlling-report?company_id={company_id}&unit_type={unit_type}"
        ).get_json()
        unit = report["units"][0]
        assert unit["total_revenue"] == "1000.00"
        assert unit["total_expense"] == "300.00"
        assert unit["net_income"] == "700.00"
        assert [
            (account["code"], account["account_type"], account["amount"])
            for account in unit["accounts"]
        ] == [("4400", "expense", "300.00"), ("8400", "income", "1000.00")]
        assert report["unassigned"]["total_revenue"] == "50.00"
        assert report["unassigned"]["net_income"] == "50.00"

    controlling_csv = client.get(
        f"/api/v1/exports/controlling.csv?company_id={company_id}&unit_type=cost_center"
    )
    assert controlling_csv.status_code == 200
    rows = list(csv.DictReader(io.StringIO(controlling_csv.get_data(as_text=True))))
    revenue_row = next(row for row in rows if row["account_code"] == "8400")
    assert revenue_row["unit_code"] == "K100"
    assert revenue_row["account_type"] == "income"
    assert revenue_row["amount"] == "1000.00"
    assert revenue_row["total_revenue"] == "1000.00"
    assert revenue_row["net_income"] == "700.00"
    unassigned_row = next(row for row in rows if row["account_code"] == "2600")
    assert unassigned_row["unit_name"] == "Nicht zugeordnet"
    assert unassigned_row["total_revenue"] == "50.00"

    page = client.get(f"/controlling?company_id={company_id}")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    # Ergebnis 700.00 je einmal in Kostenstellenrechnung und Profitcenter-GuV
    assert html.count("<strong>700.00</strong>") == 2
