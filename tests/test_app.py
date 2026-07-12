from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.auth import hash_password
from app.services.document_llm import DocumentLLMError
from domain.models import AuditLog, Company, Document, FiscalYear, Period, PeriodLock, Tenant, User


def _create_test_app(tmp_path: Path, **extra_config):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_app.db'}",
            **extra_config,
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
    return app


def _logged_in_client(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    return client


def test_index_page_loads(tmp_path):
    app = _create_test_app(tmp_path)

    client = _logged_in_client(app)
    response = client.get("/")

    assert response.status_code == 200
    assert b"OpenBuchhaltung" in response.data


def test_security_headers_are_set(tmp_path):
    app = _create_test_app(tmp_path)

    client = _logged_in_client(app)
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_session_cookie_defaults_are_hardened(tmp_path):
    app = _create_test_app(tmp_path, SESSION_COOKIE_SECURE=True)

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
    )

    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Secure" in cookie


def test_index_redirects_to_login_when_not_authenticated(tmp_path):
    app = _create_test_app(tmp_path)

    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 302
    assert "/auth/login" in response.headers["Location"]


def test_login_with_valid_credentials(tmp_path):
    app = _create_test_app(tmp_path)

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Login erfolgreich" in response.data


def test_login_with_wrong_password_is_rejected(tmp_path):
    app = _create_test_app(tmp_path)

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "falsch"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Ungültige Zugangsdaten".encode() in response.data


def test_pruefer_role_cannot_write(tmp_path):
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="pruefer",
                password_hash=hash_password("lesen123"),
                role="Pruefer",
                tenant_id=None,
            )
        )
        session.commit()

    client = app.test_client()
    client.post("/auth/login", data={"username": "pruefer", "password": "lesen123"})

    response = client.post(
        "/tenants",
        data={"tenant_name": "Mandant X", "company_name": "Mandant X GmbH"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"nur Lesezugriff" in response.data


def test_can_create_tenant_and_company_via_form(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    response = client.post(
        "/tenants",
        data={"tenant_name": "Mandant A", "company_name": "Mandant A GmbH"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Mandant und Gesellschaft wurden angelegt" in response.data
    assert b"Mandanten:</strong> 1" in response.data


def test_audit_log_page_lists_entries(tmp_path):
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Audit Mandant")
        company = Company(name="Audit GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.flush()
        session.add(
            AuditLog(
                tenant_id=tenant.id,
                company_id=company.id,
                entity_type="journal_entry",
                entity_id="42",
                action="created",
                payload={"posting_number": "2026-0001"},
                changed_by="pytest",
            )
        )
        session.commit()

    client = _logged_in_client(app)
    response = client.get("/audit-log")

    assert response.status_code == 200
    assert b"Audit-Log" in response.data
    assert b"journal_entry" in response.data
    assert b"2026-0001" in response.data


def test_can_create_account_via_form(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant B", "company_name": "Mandant B GmbH"},
        follow_redirects=True,
    )

    index_response = client.get("/")
    assert b"Mandant B GmbH" in index_response.data

    response = client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Konto wurde angelegt" in response.data
    assert b"Konten:</strong> 1" in response.data


def test_duplicate_tenant_or_company_shows_validation_message(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    first_response = client.post(
        "/tenants",
        data={"tenant_name": "Mandant C", "company_name": "Mandant C GmbH"},
        follow_redirects=True,
    )
    assert first_response.status_code == 200

    duplicate_response = client.post(
        "/tenants",
        data={"tenant_name": "Mandant C", "company_name": "Mandant C GmbH"},
        follow_redirects=True,
    )

    assert duplicate_response.status_code == 200
    assert b"Mandant oder Gesellschaft existiert bereits" in duplicate_response.data
    assert b"Mandanten:</strong> 1" in duplicate_response.data


def test_duplicate_account_code_shows_validation_message(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant D", "company_name": "Mandant D GmbH"},
        follow_redirects=True,
    )

    first_response = client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    assert first_response.status_code == 200

    duplicate_response = client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank 2",
            "account_type": "asset",
        },
        follow_redirects=True,
    )

    assert duplicate_response.status_code == 200
    assert b"Konto mit dieser Nummer existiert bereits" in duplicate_response.data
    assert b"Konten:</strong> 1" in duplicate_response.data


def test_api_create_tenant_with_company(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    response = client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant", "company_name": "Api GmbH"},
    )

    assert response.status_code == 201
    data = response.get_json()
    assert data["tenant"]["name"] == "Api Mandant"
    assert data["company"]["name"] == "Api GmbH"


def test_api_create_account_and_validate_required_fields(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant 2", "company_name": "Api GmbH 2"},
    )

    success_response = client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "1200",
            "name": "Bank API",
            "account_type": "asset",
        },
    )
    assert success_response.status_code == 201
    assert success_response.get_json()["code"] == "1200"

    invalid_response = client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1300"},
    )
    assert invalid_response.status_code == 400
    assert "required" in invalid_response.get_json()["error"]


def test_api_mcp_call_when_not_configured(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    response = client.post("/api/v1/mcp/call", json={"method": "tools/list", "params": {}})

    assert response.status_code == 503
    assert "not configured" in response.get_json()["error"]


def test_api_mcp_call_success_with_mock(tmp_path):
    app = _create_test_app(tmp_path)
    app.config["MCP_SERVER_URL"] = "http://mcp.local/rpc"
    client = _logged_in_client(app)

    with patch("app.api.mcp.call_mcp_server") as call_mock:
        call_mock.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        response = client.post(
            "/api/v1/mcp/call",
            json={"id": "1", "method": "tools/list", "params": {}},
        )

    assert response.status_code == 200
    assert response.get_json()["result"]["ok"] is True


def test_can_create_journal_entry_via_form_and_see_trial_balance(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant E", "company_name": "Mandant E GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "8400",
            "name": "Erlöse",
            "account_type": "revenue",
        },
        follow_redirects=True,
    )

    response = client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Testbuchung",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Buchung 2026-0001 wurde gespeichert" in response.data
    assert b"1200" in response.data
    assert b"8400" in response.data


def test_journal_entry_form_rejects_locked_period(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant F", "company_name": "Mandant F GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "8400",
            "name": "Erlöse",
            "account_type": "revenue",
        },
        follow_redirects=True,
    )

    with app.extensions["db_session_factory"]() as session:
        fiscal_year = FiscalYear(
            tenant_id=1,
            company_id=1,
            label="2026",
            start_date=datetime(2026, 1, 1, tzinfo=timezone.utc).date(),
            end_date=datetime(2026, 12, 31, tzinfo=timezone.utc).date(),
            is_closed=False,
        )
        session.add(fiscal_year)
        session.flush()

        period = Period(
            tenant_id=1,
            fiscal_year_id=fiscal_year.id,
            period_number=4,
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc).date(),
            end_date=datetime(2026, 4, 30, tzinfo=timezone.utc).date(),
            status="closed",
        )
        session.add(period)
        session.flush()

        session.add(
            PeriodLock(
                tenant_id=1,
                period_id=period.id,
                reason="Monatsabschluss",
                locked_by="test",
            )
        )
        session.commit()

    response = client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Gesperrte Periode",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Periode ist gesperrt" in response.data
    assert b"100.00" in response.data


def test_api_create_journal_entry_and_trial_balance(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant 3", "company_name": "Api GmbH 3"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1000", "name": "Kasse", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )

    create_response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-04",
            "description": "API Buchung",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "250.00", "credit_amount": "0.00"},
                {"account_id": 2, "debit_amount": "0.00", "credit_amount": "250.00"},
            ],
        },
    )

    assert create_response.status_code == 201
    create_payload = create_response.get_json()
    assert create_payload["posting_number"] == "2026-0001"
    assert "created_at" in create_payload

    journal_response = client.get(
        "/api/v1/journal-entries",
        query_string={"company_id": 1, "date_from": "2026-04-01", "date_to": "2026-04-30"},
    )
    assert journal_response.status_code == 200
    journal_payload = journal_response.get_json()
    assert journal_payload["entries"][0]["posting_number"] == "2026-0001"
    assert journal_payload["entries"][0]["description"] == "API Buchung"
    assert journal_payload["entries"][0]["lines"][0]["account_code"] == "1000"
    assert journal_payload["entries"][0]["lines"][1]["account_code"] == "8400"

    report_response = client.get("/api/v1/trial-balance", query_string={"company_id": 1})
    assert report_response.status_code == 200
    payload = report_response.get_json()
    assert len(payload["rows"]) == 2


def test_api_create_journal_entry_by_account_code(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Code Mandant", "company_name": "Code GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )

    # Buchung ausschließlich über Kontonummern statt interner IDs.
    create_response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-04",
            "description": "Buchung per Kontonummer",
            "status": "posted",
            "lines": [
                {"account_code": "1200", "debit_amount": "250.00"},
                {"account_code": "8400", "credit_amount": "250.00"},
            ],
        },
    )
    assert create_response.status_code == 201

    report = client.get("/api/v1/trial-balance", query_string={"company_id": 1}).get_json()
    codes = {row["code"] for row in report["rows"]}
    assert {"1200", "8400"} <= codes

    # Unbekannte Kontonummer -> Validierungsfehler (422).
    unknown = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-05",
            "description": "Falsche Kontonummer",
            "status": "posted",
            "lines": [
                {"account_code": "9999", "debit_amount": "10.00"},
                {"account_code": "8400", "credit_amount": "10.00"},
            ],
        },
    )
    assert unknown.status_code == 422

    # Weder account_id noch account_code -> Validierungsfehler (422).
    missing = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-05",
            "description": "Kein Konto",
            "status": "posted",
            "lines": [
                {"debit_amount": "10.00"},
                {"account_code": "8400", "credit_amount": "10.00"},
            ],
        },
    )
    assert missing.status_code == 422


def test_api_list_accounts(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Konten Mandant", "company_name": "Konten GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )

    # company_id ist Pflicht.
    assert client.get("/api/v1/accounts").status_code == 400

    response = client.get("/api/v1/accounts", query_string={"company_id": 1})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["company_id"] == 1
    # Nach Kontonummer sortiert, mit interner ID und Kontoart.
    codes = [account["code"] for account in payload["accounts"]]
    assert codes == ["1200", "8400"]
    bank = payload["accounts"][0]
    assert bank["name"] == "Bank"
    assert bank["account_type"] == "asset"
    assert bank["is_active"] is True
    assert isinstance(bank["id"], int)


def test_api_income_statement_respects_date_range(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Zeitraum Mandant", "company_name": "Zeitraum GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "4200", "name": "Miete", "account_type": "expense"},
    )
    # Erlös im Januar, Aufwand im Februar.
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-01-15",
            "description": "Erlös Januar",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "100.00", "credit_amount": "0.00"},
                {"account_id": 2, "debit_amount": "0.00", "credit_amount": "100.00"},
            ],
        },
    )
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-02-15",
            "description": "Miete Februar",
            "status": "posted",
            "lines": [
                {"account_id": 3, "debit_amount": "40.00", "credit_amount": "0.00"},
                {"account_id": 1, "debit_amount": "0.00", "credit_amount": "40.00"},
            ],
        },
    )

    # Ohne Zeitraum: beide Buchungen.
    full = client.get("/api/v1/income-statement", query_string={"company_id": 1}).get_json()
    assert full["period"] == {"date_from": None, "date_to": None}
    assert full["totals"]["total_revenue"] == "100.00"
    assert full["totals"]["total_expense"] == "40.00"
    assert full["totals"]["net_income"] == "60.00"

    # Nur Februar: kein Erlös, nur Aufwand.
    feb = client.get(
        "/api/v1/income-statement",
        query_string={"company_id": 1, "date_from": "2026-02-01", "date_to": "2026-02-28"},
    ).get_json()
    assert feb["period"] == {"date_from": "2026-02-01", "date_to": "2026-02-28"}
    assert feb["totals"]["total_revenue"] == "0.00"
    assert feb["totals"]["total_expense"] == "40.00"
    assert feb["totals"]["net_income"] == "-40.00"

    # Bilanz-Stichtag 31.01.: Miete (Februar) noch nicht enthalten.
    jan_bs = client.get(
        "/api/v1/balance-sheet",
        query_string={"company_id": 1, "date_to": "2026-01-31"},
    ).get_json()
    assert jan_bs["period"] == {"as_of": "2026-01-31"}
    # Bank = 100 (nur Januar-Erlös), Jahresergebnis = 100.
    bank = next(row for row in jan_bs["assets"] if row["code"] == "1200")
    assert bank["amount"] == "100.00"

    # Ungültiges Datum -> 400.
    bad = client.get(
        "/api/v1/income-statement",
        query_string={"company_id": 1, "date_from": "01.02.2026"},
    )
    assert bad.status_code == 400


def test_api_income_statement_and_balance_sheet(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant 5", "company_name": "Api GmbH 5"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1000", "name": "Kasse", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "2000", "name": "Eigenkapital", "account_type": "equity"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "4930", "name": "Buero", "account_type": "expense"},
    )

    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-04",
            "description": "Startkapital",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "1000.00", "credit_amount": "0.00"},
                {"account_id": 2, "debit_amount": "0.00", "credit_amount": "1000.00"},
            ],
        },
    )
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-05",
            "description": "Verkauf",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "200.00", "credit_amount": "0.00"},
                {"account_id": 3, "debit_amount": "0.00", "credit_amount": "200.00"},
            ],
        },
    )
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-05",
            "description": "Buerobedarf",
            "status": "posted",
            "lines": [
                {"account_id": 4, "debit_amount": "50.00", "credit_amount": "0.00"},
                {"account_id": 1, "debit_amount": "0.00", "credit_amount": "50.00"},
            ],
        },
    )

    income_statement_response = client.get(
        "/api/v1/income-statement", query_string={"company_id": 1}
    )
    assert income_statement_response.status_code == 200
    income_payload = income_statement_response.get_json()
    assert income_payload["totals"]["total_revenue"] == "200.00"
    assert income_payload["totals"]["total_expense"] == "50.00"
    assert income_payload["totals"]["net_income"] == "150.00"

    balance_sheet_response = client.get("/api/v1/balance-sheet", query_string={"company_id": 1})
    assert balance_sheet_response.status_code == 200
    balance_payload = balance_sheet_response.get_json()
    assert balance_payload["totals"]["is_balanced"] is True
    assert balance_payload["totals"]["difference"] == "0.00"
    assert balance_payload["totals"]["total_assets"] == "1150.00"
    assert balance_payload["totals"]["total_liabilities_and_equity"] == "1150.00"


def test_api_csv_exports_for_journal_and_trial_balance(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant 6", "company_name": "Api GmbH 6"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1000", "name": "Kasse", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-04",
            "description": "CSV Buchung",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "75.00", "credit_amount": "0.00"},
                {"account_id": 2, "debit_amount": "0.00", "credit_amount": "75.00"},
            ],
        },
    )

    journal_response = client.get("/api/v1/exports/journal.csv", query_string={"company_id": 1})
    assert journal_response.status_code == 200
    assert "text/csv" in journal_response.headers["Content-Type"]
    assert "posting_number,entry_date,entry_description" in journal_response.get_data(as_text=True)

    trial_balance_response = client.get(
        "/api/v1/exports/trial-balance.csv", query_string={"company_id": 1}
    )
    assert trial_balance_response.status_code == 200
    trial_balance_csv = trial_balance_response.get_data(as_text=True)
    assert "account_code,account_name,debit_total,credit_total,balance" in trial_balance_csv


def test_api_create_journal_entry_returns_422_with_field_details(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Api Mandant 4", "company_name": "Api GmbH 4"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1000", "name": "Kasse", "account_type": "asset"},
    )

    create_response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-04",
            "description": "API Buchung",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "250.00", "credit_amount": "0.00"},
                {"account_id": 1, "debit_amount": "0.00", "credit_amount": "0.00"},
            ],
        },
    )

    assert create_response.status_code == 422
    payload = create_response.get_json()
    assert payload["error"] == "Validation failed."
    assert payload["details"][0]["field"] == "journal_entry"
    assert "Betrag muss größer 0" in payload["details"][0]["message"]


def test_journal_entry_form_supports_multiple_lines(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant G", "company_name": "Mandant G GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1360",
            "name": "Geldtransit",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "8400",
            "name": "Erlöse",
            "account_type": "revenue",
        },
        follow_redirects=True,
    )

    response = client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Mehrzeilige Buchung",
            "line_account_id": ["1", "2", "3"],
            "line_side": ["debit", "debit", "credit"],
            "line_amount": ["80.00", "20.00", "100.00"],
            "line_description": ["Teil 1", "Teil 2", "Gegenkonto"],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Buchung 2026-0001 wurde gespeichert" in response.data


def test_can_upload_document_and_link_to_journal_entry(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant H", "company_name": "Mandant H GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "8400",
            "name": "Erlöse",
            "account_type": "revenue",
        },
        follow_redirects=True,
    )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Beleglink",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "journal_entry_id": "1",
            "document_file": (BytesIO(b"rechnung"), "rechnung.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Beleg wurde hochgeladen" in response.data
    assert b"rechnung.pdf" in response.data

    with app.extensions["db_session_factory"]() as session:
        document = session.query(Document).one()
        assert document.file_name == "rechnung.pdf"
        assert document.journal_entry_id == 1


def test_can_link_and_unlink_document_after_upload(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant HL", "company_name": "Mandant HL GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "8400", "name": "Erlöse", "account_type": "revenue"},
        follow_redirects=True,
    )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "Nachträglich verknüpfen",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
        follow_redirects=True,
    )
    client.post(
        "/documents",
        data={"company_id": "1", "document_file": (BytesIO(b"beleg"), "beleg.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).one().journal_entry_id is None

    link_response = client.post(
        "/documents/1/link",
        data={"company_id": "1", "journal_entry_id": "1"},
        follow_redirects=True,
    )
    assert link_response.status_code == 200
    assert "Beleg wurde mit der Buchung verknüpft".encode() in link_response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).one().journal_entry_id == 1

    unlink_response = client.post(
        "/documents/1/link",
        data={"company_id": "1", "journal_entry_id": ""},
        follow_redirects=True,
    )
    assert unlink_response.status_code == 200
    assert "Verknüpfung des Belegs wurde entfernt".encode() in unlink_response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).one().journal_entry_id is None


def test_link_document_rejects_missing_journal_entry(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant HM", "company_name": "Mandant HM GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/documents",
        data={"company_id": "1", "document_file": (BytesIO(b"beleg"), "beleg.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    response = client.post(
        "/documents/1/link",
        data={"company_id": "1", "journal_entry_id": "999"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Ausgewählte Buchung wurde nicht gefunden".encode() in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).one().journal_entry_id is None


def test_document_download_returns_file(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant I", "company_name": "Mandant I GmbH"},
        follow_redirects=True,
    )
    upload_response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"testbeleg"), "beleg.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert upload_response.status_code == 200

    response = client.get("/documents/1/download")
    assert response.status_code == 200
    assert response.data == b"testbeleg"


def test_trial_balance_csv_export_download(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant J", "company_name": "Mandant J GmbH"},
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
        follow_redirects=True,
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "8400",
            "name": "Erlöse",
            "account_type": "revenue",
        },
        follow_redirects=True,
    )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-04",
            "description": "CSV Export Buchung",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
        follow_redirects=True,
    )

    response = client.get("/reports/trial-balance.csv", query_string={"company_id": 1})
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    assert "attachment; filename=susa-1-" in response.headers["Content-Disposition"]
    csv_text = response.data.decode("utf-8")
    assert "Konto,Name,Soll,Haben,Saldo" in csv_text
    assert "1200,Bank,100.00,0.00,100.00" in csv_text
    assert "8400,Erlöse,0.00,100.00,-100.00" in csv_text


def test_document_upload_writes_audit_log(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant Audit", "company_name": "Mandant Audit GmbH"},
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"beleg-audit"), "audit.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200

    with app.extensions["db_session_factory"]() as session:
        audit_events = (
            session.query(AuditLog)
            .filter_by(entity_type="document", entity_id="1")
            .all()
        )

    assert any(event.action == "uploaded" for event in audit_events)


def test_document_upload_rejects_disallowed_extension(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant Upload", "company_name": "Mandant Upload GmbH"},
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"kein beleg"), "notiz.txt", "text/plain"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Nur PDF-, JPG- und PNG-Belege" in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).count() == 0


def test_document_upload_rejects_disallowed_mimetype(tmp_path):
    app = _create_test_app(tmp_path)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant MIME", "company_name": "Mandant MIME GmbH"},
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"kein pdf"), "rechnung.pdf", "text/plain"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Dateityp des Belegs ist nicht erlaubt" in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).count() == 0


def test_document_upload_rejects_oversized_file(tmp_path):
    app = _create_test_app(tmp_path, DOCUMENT_MAX_UPLOAD_BYTES=8)
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant Size", "company_name": "Mandant Size GmbH"},
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"x" * 16), "gross.pdf", "application/pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Beleg ist zu gro" in response.data
    with app.extensions["db_session_factory"]() as session:
        assert session.query(Document).count() == 0


def test_document_upload_calls_configured_llm_endpoint(tmp_path):
    app = _create_test_app(tmp_path)
    app.config["DOCUMENT_LLM_ENDPOINT_URL"] = "http://llm.local/v1/responses"
    app.config["DOCUMENT_LLM_MODEL"] = "gpt-4.1-mini"
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant LLM", "company_name": "Mandant LLM GmbH"},
        follow_redirects=True,
    )

    with patch("app.web.documents.send_document_update") as llm_mock:
        llm_mock.return_value = {"id": "resp_123", "status": "completed"}

        response = client.post(
            "/documents",
            data={
                "company_id": "1",
                "document_file": (BytesIO(b"llm-beleg"), "llm.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    assert response.status_code == 200
    llm_mock.assert_called_once()

    with app.extensions["db_session_factory"]() as session:
        llm_audit = session.query(AuditLog).filter_by(action="llm_update_requested").one()

    assert llm_audit.payload["status"] == "success"


def test_document_upload_continues_when_llm_endpoint_fails(tmp_path):
    app = _create_test_app(tmp_path)
    app.config["DOCUMENT_LLM_ENDPOINT_URL"] = "http://llm.local/v1/responses"
    client = _logged_in_client(app)

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant LLM2", "company_name": "Mandant LLM2 GmbH"},
        follow_redirects=True,
    )

    with patch("app.web.documents.send_document_update") as llm_mock:
        llm_mock.side_effect = DocumentLLMError("boom")

        response = client.post(
            "/documents",
            data={
                "company_id": "1",
                "document_file": (BytesIO(b"llm-fehler"), "llm-fehler.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert b"Beleg wurde hochgeladen" in response.data
