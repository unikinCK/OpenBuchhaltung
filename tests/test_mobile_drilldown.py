"""Drilldown der Übersicht (iOS-App): Geldkonten, Umsätze je Konto, Belege ohne
Buchung, überfällige Posten – in API, MCP-Pfad und Weboberfläche."""

from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app import create_app
from app.auth import hash_password
from app.services.open_items import OpenItemInput, create_open_item, settle_open_item
from domain.models import Account, Company, Tenant, User
from tests.document_files import pdf_document_bytes


def _app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'drilldown.db'}",
            "DOCUMENT_UPLOAD_DIR": str(tmp_path / "uploads"),
        }
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


def _import_csv(client, company_id: int, bank_account_id: int, rows: str) -> None:
    csv = "Buchungstag;Verwendungszweck;Betrag\n" + rows
    response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": company_id,
            "bank_account_id": bank_account_id,
            "file_name": "umsaetze.csv",
            "content_base64": base64.b64encode(csv.encode()).decode("ascii"),
        },
    )
    assert response.status_code == 201, response.get_json()


def _skr03_company(client) -> tuple[int, dict[str, int]]:
    tenant = client.post(
        "/api/v1/tenants", json={"tenant_name": "Drill", "company_name": "Drill GmbH"}
    ).get_json()
    company_id = tenant["company"]["id"]
    imported = client.post(
        "/api/v1/account-chart/import", json={"company_id": company_id, "chart": "skr03"}
    )
    assert imported.status_code == 201
    for code, name in (("1210", "Tagesgeld"), ("1220", "Festgeld")):
        created = client.post(
            "/api/v1/accounts",
            json={"company_id": company_id, "code": code, "name": name, "account_type": "asset"},
        )
        assert created.status_code == 201, created.get_json()
    accounts = {
        account["code"]: account["id"]
        for account in client.get(
            "/api/v1/accounts", query_string={"company_id": company_id}
        ).get_json()["accounts"]
    }
    return company_id, accounts


def test_dashboard_lists_all_posted_money_accounts(tmp_path):
    _, client = _app(tmp_path)
    company_id, accounts = _skr03_company(client)
    _book(client, company_id, "2025-03-01", "Erlös März", [
        {"account_code": "1200", "debit_amount": "5000.00"},
        {"account_code": "8400", "credit_amount": "5000.00"},
    ])
    _book(client, company_id, "2026-02-01", "Auf Tagesgeld", [
        {"account_code": "1210", "debit_amount": "1000.00"},
        {"account_code": "1200", "credit_amount": "1000.00"},
    ])
    _book(client, company_id, "2026-03-01", "Unterwegs", [
        {"account_code": "1360", "debit_amount": "20.00"},
        {"account_code": "1200", "credit_amount": "20.00"},
    ])
    _book(client, company_id, "2026-07-05", "API Juli", [
        {"account_code": "4900", "debit_amount": "100.00"},
        {"account_code": "1200", "credit_amount": "100.00"},
    ])
    _import_csv(client, company_id, accounts["1200"], "05.07.2026;API Juli;-100,00\n")
    fiscal_years = {
        year["label"]: year["id"]
        for year in client.get(
            "/api/v1/fiscal-years", query_string={"company_id": company_id}
        ).get_json()["fiscal_years"]
    }
    closed = client.post(f"/api/v1/fiscal-years/{fiscal_years['2025']}/close")
    assert closed.status_code == 200, closed.get_json()

    body = client.get(
        "/api/v1/dashboard", query_string={"company_id": company_id}
    ).get_json()
    rows = {row["account_code"]: row for row in body["bank_accounts"]}
    # Kasse und Festgeld sind unbebucht, Geldtransit ist kein Bankkonto.
    assert list(rows) == ["1200", "1210"]

    bank = rows["1200"]
    # Der Saldovortrag aus 2025 zählt nicht doppelt.
    assert bank["book_balance"] == "3880.00"
    assert bank["statement_total"] == "-100.00"
    assert bank["transaction_count"] == 1
    assert bank["open_transactions"] == 1
    assert bank["latest_booking_date"] == "2026-07-05"
    assert bank["latest_entry_date"] == "2026-07-05"

    savings = rows["1210"]
    assert savings["account_id"] == accounts["1210"]
    assert savings["book_balance"] == "1000.00"
    assert savings["statement_total"] == "0.00"
    assert savings["transaction_count"] == 0
    assert savings["open_transactions"] == 0
    assert savings["latest_booking_date"] is None
    assert savings["latest_entry_date"] == "2026-02-01"

    reconciliation = client.get(
        "/api/v1/bank-reconciliation", query_string={"company_id": company_id}
    ).get_json()["accounts"]
    assert [(row["account_code"], row["book_balance"]) for row in reconciliation] == [
        ("1200", "3880.00")
    ]


def test_dashboard_without_known_chart_lists_only_imported_accounts(tmp_path):
    app, client = _app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Ohne Rahmen")
        company = Company(name="Ohne Rahmen GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.flush()
        accounts = {
            code: Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type="asset",
            )
            for code, name in (("1200", "Bank"), ("1210", "Tagesgeld"), ("1400", "Forderungen"))
        }
        session.add_all(accounts.values())
        session.commit()
        company_id = company.id
        ids = {code: account.id for code, account in accounts.items()}
    _book(client, company_id, "2026-02-01", "Umbuchung", [
        {"account_code": "1210", "debit_amount": "50.00"},
        {"account_code": "1400", "credit_amount": "50.00"},
    ])
    _import_csv(client, company_id, ids["1200"], "01.02.2026;Gutschrift;10,00\n")

    body = client.get("/api/v1/dashboard", query_string={"company_id": company_id}).get_json()
    # Ohne erkennbaren Kontenrahmen beweist die Nummer 1210 nichts.
    assert [row["account_code"] for row in body["bank_accounts"]] == ["1200"]


def test_bank_transactions_filter_by_bank_account(tmp_path):
    _, client = _app(tmp_path)
    company_id, accounts = _skr03_company(client)
    _import_csv(client, company_id, accounts["1200"], "01.07.2026;Giro eins;-10,00\n")
    _import_csv(
        client,
        company_id,
        accounts["1210"],
        "02.07.2026;Zinsen;1,50\n03.07.2026;Zinsen August;1,60\n",
    )

    giro = client.get(
        "/api/v1/bank-transactions",
        query_string={"company_id": company_id, "bank_account_id": accounts["1200"]},
    ).get_json()
    assert giro["total"] == 1
    assert [tx["purpose"] for tx in giro["transactions"]] == ["Giro eins"]

    savings = client.get(
        "/api/v1/bank-transactions",
        query_string={
            "company_id": company_id,
            "bank_account_id": accounts["1210"],
            "limit": 1,
            "include_suggestions": 1,
        },
    ).get_json()
    assert savings["total"] == 2
    assert [tx["purpose"] for tx in savings["transactions"]] == ["Zinsen August"]
    assert "booking_proposal" in savings["transactions"][0]

    # Weboberfläche: Filter je Bankkonto, Absprung aus dem Saldenabgleich.
    page = client.get(
        f"/bank?company_id={company_id}&bank_account_id={accounts['1210']}"
    ).data.decode()
    assert "Zinsen August" in page
    assert "Giro eins" not in page
    overview = client.get(f"/bank?company_id={company_id}").data.decode()
    assert f"bank_account_id={accounts['1210']}" in overview


def _upload(client, company_id: int, marker: bytes, document_date: str) -> dict:
    response = client.post(
        "/api/v1/documents",
        json={
            "company_id": company_id,
            "file_name": f"{marker.decode()}.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_document_bytes(marker)).decode("ascii"),
            "document_date": document_date,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_unlinked_documents_count_current_versions_only(tmp_path):
    _, client = _app(tmp_path)
    company_id, _accounts = _skr03_company(client)
    entry = _book(client, company_id, "2026-07-01", "Bürobedarf", [
        {"account_code": "4900", "debit_amount": "10.00"},
        {"account_code": "1200", "credit_amount": "10.00"},
    ])
    linked = _upload(client, company_id, b"verknuepft", "2026-07-01")
    assert (
        client.post(
            f"/api/v1/documents/{linked['id']}/link", json={"journal_entry_id": entry["id"]}
        ).status_code
        == 200
    )
    first = _upload(client, company_id, b"erste-fassung", "2026-07-02")
    replaced = client.post(
        f"/api/v1/documents/{first['id']}/replace",
        json={
            "file_name": "zweite-fassung.pdf",
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(pdf_document_bytes(b"zweite")).decode("ascii"),
        },
    )
    assert replaced.status_code == 201
    newer = _upload(client, company_id, b"neuer-beleg", "2026-07-03")

    unlinked = client.get(
        "/api/v1/documents", query_string={"company_id": company_id, "unlinked": 1}
    ).get_json()
    assert unlinked["total"] == 2
    assert [doc["id"] for doc in unlinked["documents"]] == [
        newer["id"],
        replaced.get_json()["id"],
    ]

    page = client.get(
        "/api/v1/documents",
        query_string={"company_id": company_id, "unlinked": "true", "limit": 1, "offset": 1},
    ).get_json()
    assert page["total"] == 2
    assert [doc["id"] for doc in page["documents"]] == [replaced.get_json()["id"]]

    everything = client.get(
        "/api/v1/documents", query_string={"company_id": company_id}
    ).get_json()
    assert everything["total"] == 4

    dashboard = client.get(
        "/api/v1/dashboard", query_string={"company_id": company_id}
    ).get_json()
    assert dashboard["tasks"]["unlinked_documents"] == 2

    web = client.get(f"/belege?company_id={company_id}&unlinked=1").data.decode()
    assert "2 Belege ohne Buchung" in web
    assert "erste-fassung.pdf" not in web


def test_open_items_overdue_filter(tmp_path):
    app, client = _app(tmp_path)
    company_id, accounts = _skr03_company(client)
    today = date.today()
    with app.extensions["db_session_factory"]() as session:

        def item(reference: str, due: date | None) -> int:
            return create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company_id,
                    account_id=accounts["1400"],
                    item_type="receivable",
                    reference=reference,
                    counterparty="Kunde",
                    entry_date=today - timedelta(days=60),
                    due_date=due,
                    amount=Decimal("100.00"),
                    changed_by="test",
                ),
            ).id

        item("RE-ALT", today - timedelta(days=30))
        item("RE-GESTERN", today - timedelta(days=1))
        item("RE-HEUTE", today)
        item("RE-OHNE", None)
        settled = item("RE-BEZAHLT", today - timedelta(days=5))
        settle_open_item(session=session, open_item_id=settled, changed_by="test")
        session.commit()

    overdue = client.get(
        "/api/v1/open-items", query_string={"company_id": company_id, "overdue": 1}
    ).get_json()["open_items"]
    assert [row["reference"] for row in overdue] == ["RE-ALT", "RE-GESTERN"]

    all_open = client.get(
        "/api/v1/open-items", query_string={"company_id": company_id}
    ).get_json()["open_items"]
    assert len(all_open) == 4

    dashboard = client.get(
        "/api/v1/dashboard", query_string={"company_id": company_id}
    ).get_json()
    assert dashboard["tasks"]["overdue_open_items"] == len(overdue)

    page = client.get(f"/offene-posten?company_id={company_id}&overdue=1").data.decode()
    assert "RE-ALT" in page
    assert "RE-HEUTE" not in page
    assert "RE-OHNE" not in page

    start = client.get(f"/?company_id={company_id}").data.decode()
    assert "overdue=1" in start
    assert "unlinked=1" in start
