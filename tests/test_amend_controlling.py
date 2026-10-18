"""Kostenstelle und Profitcenter je Zeile nachträglich ergänzen (Service, API, MCP, UI)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app import create_app
from app.auth import hash_password
from app.services.mcp_server import TOOLS_BY_NAME
from domain.models import AuditLog, User


def _app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'amend.db'}"}
    )
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return app, client


def _unit(client, company_id: int, unit_type: str, code: str, **extra) -> dict:
    response = client.post(
        "/api/v1/controlling-units",
        json={
            "company_id": company_id,
            "unit_type": unit_type,
            "code": code,
            "name": f"Einheit {code}",
            **extra,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _setup(client) -> dict:
    tenant = client.post(
        "/api/v1/tenants", json={"tenant_name": "Amend", "company_name": "Amend GmbH"}
    ).get_json()
    company_id = tenant["company"]["id"]
    for code, name, account_type in (
        ("0700", "Anlagen im Bau", "asset"),
        ("1200", "Bank", "asset"),
    ):
        assert (
            client.post(
                "/api/v1/accounts",
                json={
                    "company_id": company_id,
                    "code": code,
                    "name": name,
                    "account_type": account_type,
                },
            ).status_code
            == 201
        )
    entry = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-14",
            "description": "Projektkosten",
            "lines": [
                {"account_code": "0700", "debit_amount": "1000.00"},
                {"account_code": "1200", "credit_amount": "1000.00"},
            ],
        },
    )
    assert entry.status_code == 201, entry.get_json()
    return {
        "company_id": company_id,
        "entry_id": entry.get_json()["id"],
        "cost_center": _unit(client, company_id, "cost_center", "K100"),
        "profit_center": _unit(client, company_id, "profit_center", "P100"),
    }


def _amend(client, entry_id: int, payload: dict):
    return client.patch(f"/api/v1/journal-entries/{entry_id}", json=payload)


def test_amend_sets_and_removes_cost_and_profit_center_per_line(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    cost_center, profit_center = data["cost_center"], data["profit_center"]

    response = _amend(
        client,
        data["entry_id"],
        {
            "lines": [
                {
                    "line_number": 1,
                    "cost_center_id": cost_center["id"],
                    "profit_center_id": profit_center["id"],
                }
            ]
        },
    )
    assert response.status_code == 200, response.get_json()
    lines = {line["line_number"]: line for line in response.get_json()["lines"]}
    assert lines[1]["account_code"] == "0700"
    assert (lines[1]["cost_center_code"], lines[1]["profit_center_code"]) == ("K100", "P100")
    assert (lines[2]["cost_center_id"], lines[2]["profit_center_id"]) == (None, None)
    assert lines[1]["debit_amount"] == "1000.00"

    with app.extensions["db_session_factory"]() as session:
        event = session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "journal_entry",
                AuditLog.action == "amended",
            )
        ).scalar_one()
        assert event.payload["cost_centers"] == [
            {
                "line_number": 1,
                "account_code": "0700",
                "old_id": None,
                "old_code": None,
                "new_id": cost_center["id"],
                "new_code": "K100",
            }
        ]
        assert event.payload["profit_centers"][0]["new_code"] == "P100"
        assert "partners" not in event.payload

    # Der Journalfilter findet die Buchung jetzt über die Kostenstelle.
    found = client.get(
        "/api/v1/journal-entries",
        query_string={"company_id": data["company_id"], "cost_center_id": cost_center["id"]},
    ).get_json()["entries"]
    assert [entry["id"] for entry in found] == [data["entry_id"]]

    # Nur das Profitcenter entfernen; die Kostenstelle bleibt.
    removed = _amend(
        client, data["entry_id"], {"lines": [{"line_number": 1, "profit_center_id": None}]}
    )
    assert removed.status_code == 200
    line = removed.get_json()["lines"][0]
    assert (line["cost_center_code"], line["profit_center_id"]) == ("K100", None)


def test_amend_validates_units_like_booking(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    entry_id = data["entry_id"]
    inactive = _unit(client, data["company_id"], "cost_center", "K900", is_active=False)
    future = _unit(client, data["company_id"], "cost_center", "K200", valid_from="2027-01-01")

    def error_for(line: dict) -> str:
        response = _amend(client, entry_id, {"lines": [line]})
        assert response.status_code == 422, response.get_json()
        return response.get_json()["details"][0]["message"]

    assert "inaktiv" in error_for({"line_number": 1, "cost_center_id": inactive["id"]})
    assert "noch nicht gültig" in error_for({"line_number": 1, "cost_center_id": future["id"]})
    # Profitcenter-ID als Kostenstelle: falscher Einheitentyp.
    assert "nicht gefunden" in error_for(
        {"line_number": 1, "cost_center_id": data["profit_center"]["id"]}
    )
    assert "keine Zeile 9" in error_for(
        {"line_number": 9, "cost_center_id": data["cost_center"]["id"]}
    )
    assert "integer" in error_for({"line_number": 1, "cost_center_id": "K100"})
    assert "required" in error_for({"line_number": 1})

    finalized = client.post(f"/api/v1/journal-entries/{entry_id}/finalize")
    assert finalized.status_code == 200, finalized.get_json()
    assert "festgeschrieben" in error_for(
        {"line_number": 1, "cost_center_id": data["cost_center"]["id"]}
    )


def test_mcp_amend_schema_accepts_units_per_line() -> None:
    line_schema = TOOLS_BY_NAME["amend_journal_entry"].input_schema["properties"]["lines"]
    properties = line_schema["items"]["properties"]
    assert properties["cost_center_id"]["type"] == ["integer", "null"]
    assert properties["profit_center_id"]["type"] == ["integer", "null"]


def test_detail_page_amends_units_per_line(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    entry_id = data["entry_id"]
    cost_center = data["cost_center"]

    page = client.get(f"/buchungen/{entry_id}").get_data(as_text=True)
    assert 'name="line_cost_center_1"' in page
    assert 'name="line_profit_center_2"' in page

    response = client.post(
        f"/buchungen/{entry_id}/ergaenzen",
        data={
            "line_cost_center_1": str(cost_center["id"]),
            "line_profit_center_1": "",
            "line_cost_center_2": "",
            "line_profit_center_2": "",
        },
    )
    assert response.status_code == 302
    entry = client.get(
        "/api/v1/journal-entries", query_string={"company_id": data["company_id"]}
    ).get_json()["entries"][0]
    assert entry["lines"][0]["cost_center_code"] == "K100"
    assert entry["lines"][1]["cost_center_id"] is None

    detail = client.get(f"/buchungen/{entry_id}").get_data(as_text=True)
    assert "Kostenstelle leer → K100" in detail
