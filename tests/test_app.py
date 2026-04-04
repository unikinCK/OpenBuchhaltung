from pathlib import Path

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
