from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app import create_app
from app.auth import hash_api_token, hash_password
from domain.models import Account, Company, JournalEntry, TaxCode, Tenant, User


def _create_test_app(tmp_path: Path, **extra_config):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_scoping.db'}",
            **extra_config,
        }
    )


def _seed_two_tenants_with_user(app):
    with app.extensions["db_session_factory"]() as session:
        tenant_a = Tenant(name="Tenant A")
        tenant_b = Tenant(name="Tenant B")
        company_a = Company(name="A GmbH", currency_code="EUR", tenant=tenant_a)
        company_b = Company(name="B GmbH", currency_code="EUR", tenant=tenant_b)
        session.add_all([tenant_a, tenant_b, company_a, company_b])
        session.flush()

        session.add(
            User(
                username="nutzer-a",
                password_hash=hash_password("passwort-a"),
                role="Buchhalter",
                tenant_id=tenant_a.id,
            )
        )
        session.commit()
        return company_a.id, company_b.id


def test_tenant_bound_user_sees_only_own_tenant(tmp_path):
    app = _create_test_app(tmp_path)
    company_a_id, company_b_id = _seed_two_tenants_with_user(app)

    client = app.test_client()
    client.post("/auth/login", data={"username": "nutzer-a", "password": "passwort-a"})

    response = client.get("/")
    assert response.status_code == 200
    assert b"A GmbH" in response.data
    assert b"B GmbH" not in response.data

    foreign_selection = client.get(f"/?company_id={company_b_id}")
    assert foreign_selection.status_code == 200
    assert b"B GmbH" not in foreign_selection.data


def test_tenant_bound_user_cannot_write_to_foreign_company(tmp_path):
    app = _create_test_app(tmp_path)
    _, company_b_id = _seed_two_tenants_with_user(app)

    client = app.test_client()
    client.post("/auth/login", data={"username": "nutzer-a", "password": "passwort-a"})

    response = client.post(
        "/accounts",
        data={
            "company_id": str(company_b_id),
            "code": "1200",
            "name": "Fremdes Konto",
            "account_type": "asset",
        },
    )
    assert response.status_code == 404


def test_tenant_bound_user_cannot_create_new_tenant(tmp_path):
    app = _create_test_app(tmp_path)
    _seed_two_tenants_with_user(app)

    client = app.test_client()
    client.post("/auth/login", data={"username": "nutzer-a", "password": "passwort-a"})

    response = client.post(
        "/tenants",
        data={"tenant_name": "Tenant C", "company_name": "C GmbH"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"globaler Administrator" in response.data

    with app.extensions["db_session_factory"]() as session:
        assert (
            session.execute(select(Tenant).where(Tenant.name == "Tenant C")).scalar_one_or_none()
            is None
        )


def test_api_requires_bearer_token_when_configured(tmp_path):
    app = _create_test_app(tmp_path, API_AUTH_TOKEN="geheimes-token")
    client = app.test_client()

    unauthorized = client.get("/api/v1/companies")
    assert unauthorized.status_code == 401

    wrong_token = client.get(
        "/api/v1/companies", headers={"Authorization": "Bearer falsch"}
    )
    assert wrong_token.status_code == 401

    authorized = client.get(
        "/api/v1/companies", headers={"Authorization": "Bearer geheimes-token"}
    )
    assert authorized.status_code == 200

    health = client.get("/api/v1/health")
    assert health.status_code == 200


def test_api_user_token_scopes_companies_to_tenant(tmp_path):
    token = "obk_test-token-a"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    company_a_id, _ = _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        user = session.execute(select(User).where(User.username == "nutzer-a")).scalar_one()
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
        session.commit()

    client = app.test_client()
    unauthorized = client.get("/api/v1/companies")
    assert unauthorized.status_code == 401

    response = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.get_json()
    assert [company["id"] for company in payload] == [company_a_id]
    assert payload[0]["name"] == "A GmbH"


def test_api_user_token_blocks_cross_tenant_writes_and_read_role(tmp_path):
    buchhalter_token = "obk_buchhalter-token"
    pruefer_token = "obk_pruefer-token"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    company_a_id, company_b_id = _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        tenant_a = session.get(Company, company_a_id).tenant_id
        buchhalter = session.execute(select(User).where(User.username == "nutzer-a")).scalar_one()
        buchhalter.api_token_hash = hash_api_token(buchhalter_token)
        buchhalter.api_token_last4 = buchhalter_token[-4:]
        session.add(
            User(
                username="api-pruefer",
                password_hash=hash_password("passwort"),
                role="Pruefer",
                tenant_id=tenant_a,
                api_token_hash=hash_api_token(pruefer_token),
                api_token_last4=pruefer_token[-4:],
            )
        )
        session.commit()

    client = app.test_client()
    own_write = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {buchhalter_token}"},
        json={
            "company_id": company_a_id,
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
    )
    assert own_write.status_code == 201

    foreign_write = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {buchhalter_token}"},
        json={
            "company_id": company_b_id,
            "code": "1200",
            "name": "Fremde Bank",
            "account_type": "asset",
        },
    )
    assert foreign_write.status_code == 404

    read_only_write = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {pruefer_token}"},
        json={
            "company_id": company_a_id,
            "code": "1300",
            "name": "Kasse",
            "account_type": "asset",
        },
    )
    assert read_only_write.status_code == 403


def test_api_tenant_bound_user_cannot_create_tenant(tmp_path):
    token = "obk_no-new-tenant"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        user = session.execute(select(User).where(User.username == "nutzer-a")).scalar_one()
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
        session.commit()

    client = app.test_client()
    response = client.post(
        "/api/v1/tenants",
        headers={"Authorization": f"Bearer {token}"},
        json={"tenant_name": "Tenant C", "company_name": "C GmbH"},
    )
    assert response.status_code == 403


def test_api_read_only_token_cannot_call_mcp(tmp_path):
    token = "obk_readonly-mcp"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        tenant_a_id = session.execute(
            select(Tenant.id).where(Tenant.name == "Tenant A")
        ).scalar_one()
        session.add(
            User(
                username="readonly-mcp",
                password_hash=hash_password("passwort"),
                role="Pruefer",
                tenant_id=tenant_a_id,
                api_token_hash=hash_api_token(token),
                api_token_last4=token[-4:],
            )
        )
        session.commit()

    client = app.test_client()
    response = client.post(
        "/api/v1/mcp/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"method": "tools/list", "params": {}},
    )
    assert response.status_code == 403


def test_csrf_protection_blocks_posts_without_token(tmp_path):
    app = _create_test_app(tmp_path, CSRF_PROTECT=True)
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

    # Login ohne CSRF-Token wird abgelehnt
    blocked = client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    assert blocked.status_code == 400

    # Nach GET der Login-Seite liegt ein Token in der Session
    client.get("/auth/login")
    with client.session_transaction() as flask_session:
        token = flask_session["_csrf_token"]

    ok = client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123", "_csrf_token": token},
    )
    assert ok.status_code == 302

    # Schreibende UI-Requests ohne Token werden ebenfalls abgelehnt
    blocked_write = client.post(
        "/tenants", data={"tenant_name": "T", "company_name": "C GmbH"}
    )
    assert blocked_write.status_code == 400

    ok_write = client.post(
        "/tenants",
        data={"tenant_name": "T", "company_name": "C GmbH", "_csrf_token": token},
    )
    assert ok_write.status_code == 302


def test_seed_demo_command_creates_demo_data_idempotently(tmp_path):
    app = _create_test_app(tmp_path)
    runner = app.test_cli_runner()

    first_run = runner.invoke(args=["seed-demo"])
    assert first_run.exit_code == 0, first_run.output
    assert "Demo-Daten sind bereit" in first_run.output

    second_run = runner.invoke(args=["seed-demo"])
    assert second_run.exit_code == 0, second_run.output
    assert "Beispielbuchungen übersprungen" in second_run.output

    with app.extensions["db_session_factory"]() as session:
        company = session.execute(
            select(Company).where(Company.name == "Demo GmbH")
        ).scalar_one()
        account_count = len(
            session.execute(select(Account).where(Account.company_id == company.id))
            .scalars()
            .all()
        )
        tax_code_count = len(
            session.execute(select(TaxCode).where(TaxCode.company_id == company.id))
            .scalars()
            .all()
        )
        entry_count = len(
            session.execute(
                select(JournalEntry).where(JournalEntry.company_id == company.id)
            )
            .scalars()
            .all()
        )
        user_count = len(session.execute(select(User)).scalars().all())

    assert account_count == 31
    assert tax_code_count == 5
    assert entry_count == 4
    assert user_count == 3

    from app.services.reports import balance_sheet_for_company

    with app.extensions["db_session_factory"]() as session:
        company = session.execute(
            select(Company).where(Company.name == "Demo GmbH")
        ).scalar_one()
        balance_sheet = balance_sheet_for_company(session=session, company_id=company.id)

    assert balance_sheet["totals"]["is_balanced"] is True


def test_set_api_token_command_rotates_user_token(tmp_path):
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="api-user",
                password_hash=hash_password("passwort"),
                role="Buchhalter",
            )
        )
        session.commit()

    result = app.test_cli_runner().invoke(args=["set-api-token", "--username", "api-user"])
    assert result.exit_code == 0, result.output
    assert "API-Token für api-user: obk_" in result.output

    token_line = result.output.splitlines()[0]
    token = token_line.rsplit(" ", maxsplit=1)[-1]
    with app.extensions["db_session_factory"]() as session:
        user = session.execute(select(User).where(User.username == "api-user")).scalar_one()
        assert user.api_token_last4 == token[-4:]
        assert user.api_token_hash is not None
