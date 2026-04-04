from pathlib import Path

from app import create_app
from app.services.scoping import scoped_select
from domain.models import Account, Company, Tenant


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_scoping.db'}",
        }
    )


def test_scoped_select_filters_by_company(tmp_path):
    app = _create_test_app(tmp_path)

    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Scope Tenant")
        company_a = Company(name="A GmbH", currency_code="EUR", tenant=tenant)
        company_b = Company(name="B GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company_a, company_b])
        session.flush()

        session.add_all(
            [
                Account(
                    tenant_id=tenant.id,
                    company_id=company_a.id,
                    code="1000",
                    name="A",
                    account_type="asset",
                ),
                Account(
                    tenant_id=tenant.id,
                    company_id=company_b.id,
                    code="2000",
                    name="B",
                    account_type="asset",
                ),
            ]
        )
        session.commit()

        rows = session.execute(scoped_select(Account, company_id=company_a.id)).scalars().all()

    assert len(rows) == 1
    assert rows[0].code == "1000"


def test_trial_balance_api_requires_company_scope(tmp_path):
    app = _create_test_app(tmp_path)
    client = app.test_client()

    response = client.get("/api/v1/trial-balance")

    assert response.status_code == 400
    assert "company_id" in response.get_json()["error"]
