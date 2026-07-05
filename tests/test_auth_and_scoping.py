from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app import create_app
from app.auth import hash_password
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

    assert account_count == 30
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
