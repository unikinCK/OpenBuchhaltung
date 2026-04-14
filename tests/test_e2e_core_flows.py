from io import BytesIO
from pathlib import Path

import pytest

from app import create_app
from domain.models import Account, Document


@pytest.fixture
def e2e_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_e2e.db'}",
            "DEFAULT_USER_ROLE": "Admin",
            "DEFAULT_USER_NAME": "pytest-e2e",
        }
    )


@pytest.mark.e2e
def test_e2e_happy_path_core_flow(e2e_app):
    client = e2e_app.test_client()

    tenant_response = client.post(
        "/tenants",
        data={"tenant_name": "E2E Mandant", "company_name": "E2E GmbH"},
        follow_redirects=True,
    )
    assert tenant_response.status_code == 200

    runner = e2e_app.test_cli_runner()
    import_result = runner.invoke(
        args=["import-kontenrahmen", "--company-id", "1", "--chart", "skr03"]
    )
    assert import_result.exit_code == 0
    assert "importiert=" in import_result.output

    with e2e_app.extensions["db_session_factory"]() as session:
        debit_account_id = session.query(Account.id).filter_by(company_id=1, code="1200").scalar()
        credit_account_id = session.query(Account.id).filter_by(company_id=1, code="8400").scalar()

    journal_response = client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-04-10",
            "description": "E2E Kernflow",
            "debit_account_id": str(debit_account_id),
            "credit_account_id": str(credit_account_id),
            "amount": "125.00",
        },
        follow_redirects=True,
    )
    assert journal_response.status_code == 200
    assert b"Buchung" in journal_response.data

    document_response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "journal_entry_id": "1",
            "document_file": (BytesIO(b"e2e-rechnung"), "rechnung-e2e.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert document_response.status_code == 200
    assert b"Beleg wurde hochgeladen" in document_response.data

    report_response = client.get("/reports/trial-balance.csv", query_string={"company_id": 1})
    assert report_response.status_code == 200
    assert "attachment; filename=susa-1-" in report_response.headers["Content-Disposition"]


@pytest.mark.e2e
def test_e2e_negative_journal_entry_unbalanced(e2e_app):
    client = e2e_app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "E2E Neg", "company_name": "E2E Neg GmbH", "currency_code": "EUR"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "8400", "name": "Umsatz", "account_type": "revenue"},
    )

    response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-04-10",
            "description": "Unausgeglichen",
            "status": "posted",
            "lines": [
                {"account_id": 1, "debit_amount": "100.00", "credit_amount": "0.00"},
                {"account_id": 2, "debit_amount": "0.00", "credit_amount": "90.00"},
            ],
        },
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["error"] == "Validation failed."


@pytest.mark.e2e
def test_e2e_negative_document_link_to_missing_journal_entry(e2e_app):
    client = e2e_app.test_client()

    client.post(
        "/tenants",
        data={"tenant_name": "E2E Neg Doc", "company_name": "E2E Neg Doc GmbH"},
        follow_redirects=True,
    )

    response = client.post(
        "/documents",
        data={
            "company_id": "1",
            "journal_entry_id": "999",
            "document_file": (BytesIO(b"doc"), "neg-e2e.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Ausgew" in response.data

    with e2e_app.extensions["db_session_factory"]() as session:
        assert session.query(Document).count() == 0


@pytest.mark.e2e
def test_e2e_negative_forbidden_write_for_pruefer_role(e2e_app):
    client = e2e_app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "E2E Rechte", "company_name": "E2E Rechte GmbH"},
    )

    response = client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1200", "name": "Bank", "account_type": "asset"},
        headers={"X-User-Role": "Pruefer", "X-User-Name": "pytest-pruefer"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert "Forbidden" in payload["error"]
