from pathlib import Path

from app import create_app
from app.services.account_hierarchy import parent_code_candidates, resolve_parent_account_id
from domain.models import Account, Company, Tenant


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_hierarchy.db'}",
        }
    )


def test_parent_code_candidates_follow_4_level_structure():
    assert parent_code_candidates("1234") == ["1230", "1200", "1000"]
    assert parent_code_candidates("1200") == ["1000"]
    assert parent_code_candidates("1000") == []


def test_resolve_parent_account_id_prefers_nearest_existing_parent(tmp_path):
    app = _create_test_app(tmp_path)

    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Hierarchy Mandant")
        company = Company(name="Hierarchy AG", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.flush()

        level2 = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        session.add(level2)
        session.commit()

        parent_id = resolve_parent_account_id(session=session, company_id=company.id, code="1234")

    assert parent_id == level2.id
