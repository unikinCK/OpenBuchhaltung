from pathlib import Path

from sqlalchemy import func, select

from app import create_app
from domain.models import Account


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_cli_import.db'}",
        }
    )


def _create_company(app):
    with app.extensions["db_session_factory"]() as session:
        from domain.models import Company, Tenant

        tenant = Tenant(name="CLI Mandant")
        company = Company(name="CLI GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.commit()
        return company.id


def test_cli_import_kontenrahmen_with_bundled_chart(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _create_company(app)

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "import-kontenrahmen",
            "--company-id",
            str(company_id),
            "--chart",
            "skr03",
        ]
    )

    assert result.exit_code == 0
    assert "Import abgeschlossen:" in result.output

    with app.extensions["db_session_factory"]() as session:
        account_count = session.scalar(
            select(func.count(Account.id)).where(Account.company_id == company_id)
        )

    assert account_count and account_count > 10


def test_cli_import_kontenrahmen_requires_exactly_one_source(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _create_company(app)

    runner = app.test_cli_runner()
    result_without_source = runner.invoke(
        args=["import-kontenrahmen", "--company-id", str(company_id)]
    )

    assert result_without_source.exit_code != 0
    assert "Bitte genau eine Option" in result_without_source.output

    csv_path = Path(__file__).resolve().parents[1] / "data" / "kontenrahmen" / "skr03.csv"
    result_with_both = runner.invoke(
        args=[
            "import-kontenrahmen",
            "--company-id",
            str(company_id),
            "--chart",
            "skr04",
            "--csv-path",
            str(csv_path),
        ]
    )

    assert result_with_both.exit_code != 0
    assert "Bitte genau eine Option" in result_with_both.output


def test_cli_import_kontenrahmen_skr04_preserves_quoted_names(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _create_company(app)

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "import-kontenrahmen",
            "--company-id",
            str(company_id),
            "--chart",
            "skr04",
        ]
    )

    assert result.exit_code == 0

    with app.extensions["db_session_factory"]() as session:
        account_4240 = session.scalar(
            select(Account).where(
                Account.company_id == company_id,
                Account.code == "4240",
            )
        )

    assert account_4240 is not None
    assert account_4240.name == "Gas, Strom, Wasser"
    assert account_4240.account_type == "expense"
