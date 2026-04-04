from pathlib import Path
from unittest.mock import patch

from app import create_app


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
    assert b"value=\"1\">Mandant B GmbH" in index_response.data

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
