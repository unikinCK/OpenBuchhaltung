from io import StringIO
from pathlib import Path

from sqlalchemy import func, select

from app import create_app
from domain.models import Account

CSV_CONTENT = """Kontonummer,Bezeichnung,Kontoart
1000,Kasse,asset
1200,Bank,asset
1200,Bank Duplicate,asset
3300,,expense
"""


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_import.db'}",
        }
    )


def _create_company(app):
    with app.extensions["db_session_factory"]() as session:
        from domain.models import Company, Tenant

        tenant = Tenant(name="Import Mandant")
        company = Company(name="Import GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.commit()
        return company.id


def test_import_account_chart_csv_success_duplicate_and_invalid_row(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _create_company(app)

    from app.services.account_chart_import import import_account_chart_csv

    with app.extensions["db_session_factory"]() as session:
        report = import_account_chart_csv(
            session=session,
            company_id=company_id,
            csv_stream=StringIO(CSV_CONTENT),
        )

        accounts = session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        ).scalars().all()

    assert report.total_rows == 4
    assert report.imported_rows == 2
    assert report.duplicate_rows == 1
    assert report.error_rows == 1
    assert len(report.errors) == 1
    assert "Pflichtfelder fehlen" in report.errors[0].message

    assert [account.code for account in accounts] == ["1000", "1200"]
    assert accounts[0].hierarchy_level == 1
    assert accounts[1].hierarchy_level == 2
    assert accounts[1].parent_account_id == accounts[0].id

    with app.extensions["db_session_factory"]() as session:
        second_report = import_account_chart_csv(
            session=session,
            company_id=company_id,
            csv_stream=StringIO(CSV_CONTENT),
        )

        account_count = session.scalar(
            select(func.count(Account.id)).where(Account.company_id == company_id)
        )

    assert second_report.imported_rows == 0
    assert second_report.duplicate_rows == 3
    assert second_report.error_rows == 1
    assert account_count == 2
