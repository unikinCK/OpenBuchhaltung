"""Suche/Filter der Massendaten-Seiten und Dashboard-Aufgabenkacheln."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from app import create_app
from app.auth import hash_password
from app.services.bank_import import import_bank_csv
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item
from domain.models import Account, Company, Tenant, User


def _create_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'search.db'}",
        }
    )


def _seed(app):
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Such Tenant")
        company = Company(name="Such GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.flush()
        bank = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        rent = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="4200",
            name="Miete",
            account_type="expense",
        )
        receivable = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1400",
            name="Forderungen",
            account_type="asset",
        )
        session.add_all([bank, rent, receivable])
        session.add(
            User(
                username="sucher",
                password_hash=hash_password("passwort"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.flush()

        import_bank_csv(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            csv_stream=StringIO(
                "Buchungstag;Verwendungszweck;Betrag\n"
                "05.07.2026;Miete Juli;-595,00\n"
                "06.07.2026;Serverkosten;-49,00\n"
            ),
            changed_by="seed",
        )
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 7, 6),
                description="Servermiete Juli",
                status="posted",
                changed_by="seed",
                lines=[
                    JournalLineInput(rent.id, Decimal("49.00"), Decimal("0.00")),
                    JournalLineInput(bank.id, Decimal("0.00"), Decimal("49.00")),
                ],
            ),
        )
        create_open_item(
            session=session,
            payload=OpenItemInput(
                company_id=company.id,
                account_id=receivable.id,
                item_type="receivable",
                reference="RE-77",
                counterparty="Kunde Überfällig",
                entry_date=date.today() - timedelta(days=40),
                due_date=date.today() - timedelta(days=10),
                amount=Decimal("100.00"),
                changed_by="seed",
            ),
        )
        session.commit()
        return company.id


def _login(app):
    client = app.test_client()
    client.post("/auth/login", data={"username": "sucher", "password": "passwort"})
    return client


def test_bank_page_filters_by_query_and_date(tmp_path):
    app = _create_app(tmp_path)
    company_id = _seed(app)
    client = _login(app)

    filtered = client.get(f"/bank?company_id={company_id}&q=Server")
    assert filtered.status_code == 200
    assert "Serverkosten".encode() in filtered.data
    assert "Miete Juli".encode() not in filtered.data

    dated = client.get(f"/bank?company_id={company_id}&date_to=2026-07-05")
    assert "Miete Juli".encode() in dated.data
    assert "Serverkosten".encode() not in dated.data


def test_journal_and_open_items_filter_by_query(tmp_path):
    app = _create_app(tmp_path)
    company_id = _seed(app)
    client = _login(app)

    journal = client.get(f"/buchungen?company_id={company_id}&q=Servermiete")
    assert "Servermiete Juli".encode() in journal.data

    no_hit = client.get(f"/buchungen?company_id={company_id}&q=NichtVorhanden")
    assert "Servermiete Juli".encode() not in no_hit.data

    opos = client.get(f"/offene-posten?company_id={company_id}&q=RE-77")
    assert b"RE-77" in opos.data
    opos_none = client.get(f"/offene-posten?company_id={company_id}&q=RE-99")
    assert b"RE-77" not in opos_none.data


def test_bank_api_supports_query_filter(tmp_path):
    app = _create_app(tmp_path)
    company_id = _seed(app)
    client = _login(app)

    response = client.get(f"/api/v1/bank-transactions?company_id={company_id}&q=Server")
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1
    assert body["transactions"][0]["purpose"] == "Serverkosten"

    bad_date = client.get(
        f"/api/v1/bank-transactions?company_id={company_id}&date_from=kein-datum"
    )
    assert bad_date.status_code == 400


def test_dashboard_shows_open_task_tiles(tmp_path):
    app = _create_app(tmp_path)
    company_id = _seed(app)
    client = _login(app)

    response = client.get(f"/?company_id={company_id}")
    assert response.status_code == 200
    page = response.data.decode()
    assert "Offene Aufgaben" in page
    assert "Unverbuchte Bankumsätze" in page
    assert "Überfällige offene Posten" in page
    # Ein offener Umsatz ist verbucht? Nein: beide offen; ein OPOS überfällig.
    with app.extensions["db_session_factory"]() as session:
        from app.web.dashboard import open_task_counts

        tasks = open_task_counts(session, company_id)
    assert tasks["open_bank_transactions"] == 2
    assert tasks["overdue_open_items"] == 1
    assert tasks["unlinked_documents"] == 0
    assert tasks["pending_match_suggestions"] == 0
