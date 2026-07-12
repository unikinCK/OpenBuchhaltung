from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app import create_app
from app.auth import hash_api_token, hash_password
from domain.models import Account, AuditLog, Company, JournalEntry, TaxCode, Tenant, User


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


def test_api_user_token_scopes_audit_log_to_tenant(tmp_path):
    token = "obk_audit-scope"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    company_a_id, company_b_id = _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        user = session.execute(select(User).where(User.username == "nutzer-a")).scalar_one()
        user.api_token_hash = hash_api_token(token)
        user.api_token_last4 = token[-4:]
        company_a = session.get(Company, company_a_id)
        company_b = session.get(Company, company_b_id)
        session.add_all(
            [
                AuditLog(
                    tenant_id=company_a.tenant_id,
                    company_id=company_a.id,
                    entity_type="journal_entry",
                    entity_id="1",
                    action="created",
                    changed_by="a",
                ),
                AuditLog(
                    tenant_id=company_b.tenant_id,
                    company_id=company_b.id,
                    entity_type="journal_entry",
                    entity_id="2",
                    action="created",
                    changed_by="b",
                ),
            ]
        )
        session.commit()

    client = app.test_client()
    response = client.get("/api/v1/audit-log", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.get_json()
    assert [entry["entity_id"] for entry in payload["entries"]] == ["1"]

    foreign_filter = client.get(
        f"/api/v1/audit-log?company_id={company_b_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert foreign_filter.status_code == 404


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


def test_support_api_token_reads_across_tenants_but_cannot_write(tmp_path):
    token = "obk_support-token"
    app = _create_test_app(tmp_path, API_REQUIRE_AUTH=True)
    _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="support",
                password_hash=hash_password("passwort"),
                role="Support",
                tenant_id=None,
                api_token_hash=hash_api_token(token),
                api_token_last4=token[-4:],
            )
        )
        session.commit()

    client = app.test_client()
    companies = client.get("/api/v1/companies", headers={"Authorization": f"Bearer {token}"})
    assert companies.status_code == 200
    assert {company["name"] for company in companies.get_json()} == {"A GmbH", "B GmbH"}

    write = client.post(
        "/api/v1/accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "company_id": companies.get_json()[0]["id"],
            "code": "1200",
            "name": "Bank",
            "account_type": "asset",
        },
    )
    assert write.status_code == 403

    create_tenant = client.post(
        "/api/v1/tenants",
        headers={"Authorization": f"Bearer {token}"},
        json={"tenant_name": "Tenant C", "company_name": "C GmbH"},
    )
    assert create_tenant.status_code == 403

    create_user = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "x", "password": "secret", "role": "Pruefer"},
    )
    assert create_user.status_code == 403


def test_user_admin_api_create_rotate_and_disable(tmp_path):
    app = _create_test_app(tmp_path)
    company_a_id, _ = _seed_two_tenants_with_user(app)
    client = app.test_client()

    with app.extensions["db_session_factory"]() as session:
        tenant_id = session.get(Company, company_a_id).tenant_id

    created = client.post(
        "/api/v1/users",
        json={
            "username": "api-buchhalter",
            "password": "start123",
            "role": "Buchhalter",
            "tenant_id": tenant_id,
        },
    )
    assert created.status_code == 201
    user_id = created.get_json()["id"]

    rotated = client.post(f"/api/v1/users/{user_id}/api-token", json={})
    assert rotated.status_code == 201
    rotated_payload = rotated.get_json()
    assert rotated_payload["api_token"].startswith("obk_")
    assert rotated_payload["api_token_last4"] == rotated_payload["api_token"][-4:]

    disabled = client.post(f"/api/v1/users/{user_id}/active", json={"is_active": False})
    assert disabled.status_code == 200
    assert disabled.get_json()["is_active"] is False

    listed = client.get("/api/v1/users")
    assert listed.status_code == 200
    assert any(user["username"] == "api-buchhalter" for user in listed.get_json()["users"])


def test_admin_ui_manages_users(tmp_path):
    app = _create_test_app(tmp_path)
    _seed_two_tenants_with_user(app)
    with app.extensions["db_session_factory"]() as session:
        session.add(
            User(
                username="global-admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.commit()

    client = app.test_client()
    client.post("/auth/login", data={"username": "global-admin", "password": "admin123"})
    created = client.post(
        "/users",
        data={"username": "ui-pruefer", "password": "start123", "role": "Pruefer"},
        follow_redirects=True,
    )
    assert created.status_code == 200
    assert b"ui-pruefer" in created.data

    with app.extensions["db_session_factory"]() as session:
        user_id = session.execute(select(User.id).where(User.username == "ui-pruefer")).scalar_one()

    rotated = client.post(f"/users/{user_id}/api-token", follow_redirects=True)
    assert rotated.status_code == 200
    assert b"API-Token f" in rotated.data
    assert b"obk_" in rotated.data

    disabled = client.post(
        f"/users/{user_id}/active",
        data={"is_active": "false"},
        follow_redirects=True,
    )
    assert disabled.status_code == 200
    with app.extensions["db_session_factory"]() as session:
        assert session.get(User, user_id).is_active is False


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
    assert user_count == 4

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
