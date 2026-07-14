from io import StringIO
from pathlib import Path

from sqlalchemy import func, select

from app import create_app
from app.auth import hash_password
from domain.models import Account, AuditLog, User

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
        account_events = session.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "account")
            .order_by(AuditLog.sequence_number)
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
    assert [event.action for event in account_events] == ["created", "created"]
    assert account_events[0].payload["before"] is None
    assert account_events[0].payload["after"]["code"] == "1000"

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


def test_account_chart_api_imports_bundled_chart(tmp_path):
    app = _create_test_app(tmp_path)
    company_id = _create_company(app)
    client = app.test_client()

    response = client.post(
        "/api/v1/account-chart/import",
        json={"company_id": company_id, "chart": "skr03"},
    )
    assert response.status_code == 201
    report = response.get_json()["report"]
    assert report["imported_rows"] > 0
    assert report["error_rows"] == 0

    with app.extensions["db_session_factory"]() as session:
        account_count = session.scalar(
            select(func.count(Account.id)).where(Account.company_id == company_id)
        )
    assert account_count == report["imported_rows"]


def test_account_chart_ui_imports_bundled_chart(tmp_path):
    app = _create_test_app(tmp_path)
    _create_company(app)
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
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    response = client.post(
        "/accounts/import-chart",
        data={"company_id": "1", "chart": "skr04"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Kontenrahmen-Import" in response.data

    with app.extensions["db_session_factory"]() as session:
        account_count = session.scalar(
            select(func.count(Account.id)).where(Account.company_id == 1)
        )
    assert account_count > 0
