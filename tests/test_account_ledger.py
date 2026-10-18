"""Kontoblatt: Buchungen eines Kontos in Service, API, MCP-Pfad und Oberfläche."""

from __future__ import annotations

from pathlib import Path

from app import create_app
from app.auth import hash_password
from domain.models import User


def _app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'ledger.db'}"}
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


def _book(client, company_id: int, entry_date: str, description: str, lines: list) -> dict:
    response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": entry_date,
            "description": description,
            "lines": lines,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _setup(client) -> dict:
    tenant = client.post(
        "/api/v1/tenants", json={"tenant_name": "Ledger", "company_name": "Ledger GmbH"}
    ).get_json()
    company_id = tenant["company"]["id"]
    assert (
        client.post(
            "/api/v1/account-chart/import", json={"company_id": company_id, "chart": "skr03"}
        ).status_code
        == 201
    )
    accounts = {
        account["code"]: account["id"]
        for account in client.get(
            "/api/v1/accounts", query_string={"company_id": company_id}
        ).get_json()["accounts"]
    }
    sale_2025 = _book(
        client,
        company_id,
        "2025-03-01",
        "Erlös März",
        [
            {"account_code": "1200", "debit_amount": "500.00"},
            {"account_code": "8400", "credit_amount": "500.00"},
        ],
    )
    expense = _book(
        client,
        company_id,
        "2026-07-05",
        "OpenAI API Juli",
        [
            {"account_code": "4900", "debit_amount": "100.00"},
            {"account_code": "1200", "credit_amount": "100.00"},
        ],
    )
    sale_2026 = _book(
        client,
        company_id,
        "2026-08-01",
        "Erlös August",
        [
            {"account_code": "1200", "debit_amount": "50.00"},
            {"account_code": "8400", "credit_amount": "50.00"},
        ],
    )
    fiscal_years = {
        year["label"]: year["id"]
        for year in client.get(
            "/api/v1/fiscal-years", query_string={"company_id": company_id}
        ).get_json()["fiscal_years"]
    }
    return {
        "company_id": company_id,
        "accounts": accounts,
        "fiscal_years": fiscal_years,
        "entries": {"sale_2025": sale_2025, "expense": expense, "sale_2026": sale_2026},
    }


def _ledger(client, account_id: int, **params) -> dict:
    response = client.get(f"/api/v1/accounts/{account_id}/ledger", query_string=params)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_ledger_shows_opening_balance_running_balance_and_contra_accounts(
    tmp_path: Path,
) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    bank_id = data["accounts"]["1200"]

    ledger = _ledger(client, bank_id, fiscal_year_id=data["fiscal_years"]["2026"])
    assert ledger["account"]["code"] == "1200"
    assert ledger["period"]["date_from"] == "2026-01-01"
    assert ledger["period"]["fiscal_year"] == "2026"
    assert ledger["opening_balance"] == "500.00"
    assert ledger["total_lines"] == 2
    assert [
        (line["posting_number"], line["debit_amount"], line["credit_amount"], line["balance"])
        for line in ledger["lines"]
    ] == [
        (data["entries"]["expense"]["posting_number"], "0.00", "100.00", "400.00"),
        (data["entries"]["sale_2026"]["posting_number"], "50.00", "0.00", "450.00"),
    ]
    assert [line["contra_accounts"][0]["code"] for line in ledger["lines"]] == ["4900", "8400"]
    assert ledger["lines"][0]["contra_accounts"][0]["account_id"] == data["accounts"]["4900"]
    assert ledger["totals"] == {
        "debit_total": "50.00",
        "credit_total": "100.00",
        "closing_balance": "450.00",
    }

    # Ohne Zeitraum: alle Jahre, Anfangssaldo null.
    everything = _ledger(client, bank_id)
    assert everything["opening_balance"] == "0.00"
    assert [line["balance"] for line in everything["lines"]] == ["500.00", "400.00", "450.00"]

    # Blättern: der laufende Saldo zählt die Zeilen vor der Seite mit.
    page = _ledger(client, bank_id, limit=1, offset=2)
    assert page["total_lines"] == 3
    assert [line["balance"] for line in page["lines"]] == ["450.00"]


def test_income_accounts_restart_each_fiscal_year(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    revenue_id = data["accounts"]["8400"]

    ledger_2026 = _ledger(client, revenue_id, date_from="2026-01-01", date_to="2026-12-31")
    assert ledger_2026["opening_balance"] == "0.00"
    assert [line["balance"] for line in ledger_2026["lines"]] == ["-50.00"]

    mid_year = _ledger(client, revenue_id, date_from="2026-08-01")
    assert mid_year["opening_balance"] == "0.00"
    assert mid_year["totals"]["closing_balance"] == "-50.00"


def test_carryforward_is_not_shown_as_line(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    closed = client.post(f"/api/v1/fiscal-years/{data['fiscal_years']['2025']}/close")
    assert closed.status_code == 200, closed.get_json()
    assert closed.get_json()["opening_balance_entry_id"] is not None

    ledger = _ledger(client, data["accounts"]["1200"], date_from="2026-01-01")
    assert ledger["opening_balance"] == "500.00"
    assert ledger["total_lines"] == 2
    assert ledger["totals"]["closing_balance"] == "450.00"


def test_ledger_validation_and_scoping(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    bank_id = data["accounts"]["1200"]

    assert client.get(f"/api/v1/accounts/{bank_id}/ledger?date_from=gestern").status_code == 400
    reversed_range = client.get(
        f"/api/v1/accounts/{bank_id}/ledger",
        query_string={"date_from": "2026-12-31", "date_to": "2026-01-01"},
    )
    assert reversed_range.status_code == 400
    assert client.get("/api/v1/accounts/999999/ledger").status_code == 404

    other = client.post(
        "/api/v1/tenants", json={"tenant_name": "Andere", "company_name": "Andere GmbH"}
    ).get_json()
    other_years = client.post(
        "/api/v1/fiscal-years",
        json={
            "company_id": other["company"]["id"],
            "label": "2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )
    assert other_years.status_code == 201, other_years.get_json()
    foreign_year = other_years.get_json()["id"]
    assert (
        client.get(
            f"/api/v1/accounts/{bank_id}/ledger", query_string={"fiscal_year_id": foreign_year}
        ).status_code
        == 400
    )


def test_journal_and_reports_expose_account_ids(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]

    journal = client.get(
        "/api/v1/journal-entries",
        query_string={"company_id": company_id, "account_id": data["accounts"]["4900"]},
    ).get_json()
    assert journal["account_id"] == data["accounts"]["4900"]
    assert [entry["description"] for entry in journal["entries"]] == ["OpenAI API Juli"]

    trial_balance = client.get(
        "/api/v1/trial-balance", query_string={"company_id": company_id}
    ).get_json()
    by_code = {row["code"]: row for row in trial_balance["rows"]}
    assert by_code["1200"]["account_id"] == data["accounts"]["1200"]

    income = client.get(
        "/api/v1/income-statement", query_string={"company_id": company_id}
    ).get_json()
    assert {row["account_id"] for row in income["revenues"]} == {data["accounts"]["8400"]}
    balance_sheet = client.get(
        "/api/v1/balance-sheet", query_string={"company_id": company_id}
    ).get_json()
    assert balance_sheet["assets"][0]["account_id"] == data["accounts"]["1200"]


def test_account_detail_page_and_links(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    bank_id = data["accounts"]["1200"]
    detail_url = f"/konten/{bank_id}"

    page = client.get(detail_url, query_string={"fiscal_year_id": data["fiscal_years"]["2026"]})
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Kontoblatt" in html
    assert "Anfangssaldo" in html
    assert "500,00&nbsp;S" in html
    assert "450,00&nbsp;S" in html
    assert data["entries"]["expense"]["posting_number"] in html
    assert f'href="/konten/{data["accounts"]["4900"]}"' in html

    all_years = client.get(detail_url, query_string={"fiscal_year_id": "alle"})
    assert data["entries"]["sale_2025"]["posting_number"] in all_years.get_data(as_text=True)
    assert client.get("/konten/999999").status_code == 404

    accounts_page = client.get("/konten", query_string={"company_id": company_id})
    assert f'href="{detail_url}"' in accounts_page.get_data(as_text=True)

    journal_page = client.get("/buchungen", query_string={"company_id": company_id})
    assert f'href="{detail_url}"' in journal_page.get_data(as_text=True)

    filtered = client.get(
        "/buchungen",
        query_string={"company_id": company_id, "account_id": data["accounts"]["4900"]},
    ).get_data(as_text=True)
    assert "OpenAI API Juli" in filtered
    assert "Erlös August" not in filtered

    reports = client.get("/berichte", query_string={"company_id": company_id})
    assert f"/konten/{bank_id}?" in reports.get_data(as_text=True)

    entry_id = data["entries"]["expense"]["id"]
    entry_page = client.get(f"/buchungen/{entry_id}")
    assert f'href="{detail_url}"' in entry_page.get_data(as_text=True)

    renamed = client.post(
        f"/accounts/{bank_id}/update",
        data={"name": "Hausbank", "is_active": "true", "next": "detail"},
    )
    assert renamed.status_code == 302
    assert renamed.headers["Location"].endswith(detail_url)


def test_account_list_filters_and_ledger_by_account_code(tmp_path: Path) -> None:
    _, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]

    def codes(**params) -> list[str]:
        response = client.get(
            "/api/v1/accounts", query_string={"company_id": company_id, **params}
        )
        assert response.status_code == 200
        return [account["code"] for account in response.get_json()["accounts"]]

    assert codes(code="4900") == ["4900"]
    assert codes(code="49") == []
    assert "4900" in codes(q="49")
    assert "Sonstige betriebliche Aufwendungen" in [
        account["name"]
        for account in client.get(
            "/api/v1/accounts", query_string={"company_id": company_id, "q": "betriebliche"}
        ).get_json()["accounts"]
    ]
    expense = client.get(
        "/api/v1/accounts", query_string={"company_id": company_id, "account_type": "expense"}
    ).get_json()["accounts"]
    assert expense and {account["account_type"] for account in expense} == {"expense"}
    assert "1200" not in codes(account_type="expense")

    by_code = client.get(
        "/api/v1/accounts/ledger",
        query_string={
            "company_id": company_id,
            "account_code": "4900",
            "fiscal_year_id": data["fiscal_years"]["2026"],
        },
    )
    assert by_code.status_code == 200, by_code.get_json()
    ledger = by_code.get_json()
    assert ledger["account"]["id"] == data["accounts"]["4900"]
    assert [line["description"] for line in ledger["lines"]] == ["OpenAI API Juli"]

    by_id = client.get(
        "/api/v1/accounts/ledger", query_string={"account_id": data["accounts"]["4900"]}
    )
    assert by_id.status_code == 200
    assert by_id.get_json()["account"]["code"] == "4900"

    missing = client.get(
        "/api/v1/accounts/ledger",
        query_string={"company_id": company_id, "account_code": "4985"},
    )
    assert missing.status_code == 404
    assert client.get("/api/v1/accounts/ledger").status_code == 400
    assert (
        client.get("/api/v1/accounts/ledger", query_string={"account_code": "4900"}).status_code
        == 400
    )
    # Konto-ID einer anderen Gesellschaft passt nicht zur angegebenen company_id.
    assert (
        client.get(
            "/api/v1/accounts/ledger",
            query_string={"company_id": company_id + 999, "account_id": data["accounts"]["4900"]},
        ).status_code
        == 404
    )
