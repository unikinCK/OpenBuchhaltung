from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.services.document_llm import DocumentLLMError
from domain.models import AuditLog, Document, FiscalYear, Period, PeriodLock


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_app.db'}",
        }
    )


def test_index_page_loads(tmp_path):
    app = _create_test_app(tmp_path)

    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"OpenBuchhaltung" in response.data


def test_login_with_valid_credentials(tmp_path):
    app = _create_test_app(tmp_path)

    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Login erfolgreich" in response.data


def test_can_create_tenant_and_company_via_form(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    response = client.post(
        "/tenants",
        data={"tenant_name": "Mandant A", "company_name": "Mandant A GmbH"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Mandant und Gesellschaft wurden angelegt" in response.data
    assert b"Mandanten:</strong> 1" in response.data


def test_can_create_account_via_form(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

    response = client.post("/api/v1/mcp/call", json={"method": "tools/list", "params": {}})

    assert response.status_code == 503
    assert "not configured" in response.get_json()["error"]


def test_api_mcp_call_success_with_mock(tmp_path):
    app = _create_test_app(tmp_path)
    app.config["MCP_SERVER_URL"] = "http://mcp.local/rpc"
    client = app.test_client()

    with patch("app.api.call_mcp_server") as call_mock:
        call_mock.return_value = {"jsonrpc": "2.0", "id": "1", "result": {"ok": True}}
        response = client.post(
            "/api/v1/mcp/call",
            json={"id": "1", "method": "tools/list", "params": {}},
        )

    assert response.status_code == 200
    assert response.get_json()["result"]["ok"] is True


def test_can_create_journal_entry_via_form_and_see_trial_balance(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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

    report_response = client.get("/api/v1/trial-balance", query_string={"company_id": 1})
    assert report_response.status_code == 200
    payload = report_response.get_json()
    assert len(payload["rows"]) == 2


def test_api_income_statement_and_balance_sheet(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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
    client = app.test_client()

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


def test_document_download_returns_file(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant I", "company_name": "Mandant I GmbH"},
        follow_redirects=True,
    )
    upload_response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "document_file": (BytesIO(b"testbeleg"), "beleg.txt"),
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
    client = app.test_client()

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
    client = app.test_client()

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


def test_document_upload_calls_configured_llm_endpoint(tmp_path):
    app = _create_test_app(tmp_path)
    app.config["DOCUMENT_LLM_ENDPOINT_URL"] = "http://llm.local/v1/responses"
    app.config["DOCUMENT_LLM_MODEL"] = "gpt-4.1-mini"
    client = app.test_client()

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant LLM", "company_name": "Mandant LLM GmbH"},
        follow_redirects=True,
    )

    with patch("app.main.send_document_update") as llm_mock:
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
    client = app.test_client()

    client.post(
        "/tenants",
        data={"tenant_name": "Mandant LLM2", "company_name": "Mandant LLM2 GmbH"},
        follow_redirects=True,
    )

    with patch("app.main.send_document_update") as llm_mock:
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
