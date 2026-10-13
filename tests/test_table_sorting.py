"""Spaltensortierung der Tabellen: serverseitig für paginierte Listen, sonst im Browser."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from app import create_app
from app.auth import hash_password
from app.services.account_ledger import AccountLedgerError, account_ledger
from app.services.bank_import import import_bank_csv
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.mcp_server import TOOLS_BY_NAME
from app.services.open_items import OpenItemInput, create_open_item
from app.services.partners import create_partner
from domain.models import Account, Company, Document, Tenant, User


def _create_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'sorting.db'}",
        }
    )


def _seed(app) -> dict:
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="Sortier Tenant")
        company = Company(name="Sortier GmbH", currency_code="EUR", tenant=tenant)
        session.add_all([tenant, company])
        session.flush()

        def account(code: str, name: str, account_type: str) -> Account:
            item = Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
            )
            session.add(item)
            return item

        bank = account("1200", "Bank", "asset")
        receivable = account("1400", "Forderungen", "asset")
        rent = account("4210", "Miete", "expense")
        office = account("4930", "Bürobedarf", "expense")
        session.add(
            User(
                username="sortierer",
                password_hash=hash_password("passwort"),
                role="Admin",
                tenant_id=None,
            )
        )
        session.flush()

        zebra = create_partner(
            session=session,
            company=company,
            changed_by="seed",
            name="Zebra Papier",
            is_supplier=True,
            city="Aachen",
        )
        alpha = create_partner(
            session=session,
            company=company,
            changed_by="seed",
            name="Alpha Miete",
            is_customer=True,
            city="Zwickau",
        )
        session.flush()

        def book(day: int, description: str, amount: str, expense: Account, partner=None):
            return create_journal_entry(
                session=session,
                payload=JournalEntryInput(
                    company_id=company.id,
                    entry_date=date(2026, 7, day),
                    description=description,
                    status="posted",
                    changed_by="seed",
                    lines=[
                        JournalLineInput(
                            expense.id,
                            Decimal(amount),
                            Decimal("0.00"),
                            partner_id=partner.id if partner else None,
                        ),
                        JournalLineInput(bank.id, Decimal("0.00"), Decimal(amount)),
                    ],
                ),
            )

        book(1, "Kopierpapier", "30.00", office, zebra)
        book(2, "Miete Juli", "900.00", rent, alpha)
        book(3, "Briefmarken", "5.00", office)

        import_bank_csv(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            csv_stream=StringIO(
                "Buchungstag;Verwendungszweck;Betrag\n"
                "05.07.2026;Umsatz Mittel;-200,00\n"
                "06.07.2026;Umsatz Klein;-10,00\n"
                "07.07.2026;Umsatz Gross;-5000,00\n"
            ),
            changed_by="seed",
        )

        for reference, amount, due_offset in (
            ("OP-Mitte", "50.00", 20),
            ("OP-Gross", "700.00", 5),
            ("OP-Klein", "1.00", 40),
        ):
            create_open_item(
                session=session,
                payload=OpenItemInput(
                    company_id=company.id,
                    account_id=receivable.id,
                    item_type="receivable",
                    reference=reference,
                    counterparty="Kunde",
                    entry_date=date.today(),
                    due_date=date.today() + timedelta(days=due_offset),
                    amount=Decimal(amount),
                    changed_by="seed",
                ),
            )

        for file_name, document_date in (
            ("b-rechnung.pdf", date(2026, 7, 9)),
            ("c-quittung.pdf", date(2026, 7, 1)),
            ("a-vertrag.pdf", date(2026, 7, 5)),
        ):
            session.add(
                Document(
                    tenant_id=tenant.id,
                    company_id=company.id,
                    file_name=file_name,
                    storage_key=f"test/{file_name}",
                    mime_type="application/pdf",
                    document_date=document_date,
                    uploaded_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
                )
            )
        session.commit()
        return {"company_id": company.id, "bank_id": bank.id, "office_id": office.id}


@pytest.fixture()
def setup(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    client = app.test_client()
    client.post("/auth/login", data={"username": "sortierer", "password": "passwort"})
    return app, client, ids


def _order(html: str, *markers: str) -> list[str]:
    """Marker in der Reihenfolge ihres ersten Vorkommens im Tabellenrumpf."""
    body = html[html.index("<tbody>") :]
    positions = {marker: body.find(marker) for marker in markers}
    missing = [marker for marker, position in positions.items() if position < 0]
    assert not missing, missing
    return sorted(markers, key=positions.get)


def test_journal_sorts_by_every_column(setup):
    _, client, ids = setup
    url = f"/buchungen?company_id={ids['company_id']}"
    entries = ("Kopierpapier", "Miete Juli", "Briefmarken")

    html = client.get(url).get_data(as_text=True)
    assert _order(html, *entries) == ["Briefmarken", "Miete Juli", "Kopierpapier"]
    assert 'data-sort-server data-sort="date" data-sort-dir="desc"' in html
    assert 'data-sort-key="amount"' in html

    html = client.get(f"{url}&sort=amount&dir=asc").get_data(as_text=True)
    assert _order(html, *entries) == ["Briefmarken", "Kopierpapier", "Miete Juli"]
    assert 'data-sort="amount" data-sort-dir="asc"' in html

    html = client.get(f"{url}&sort=text&dir=asc").get_data(as_text=True)
    assert _order(html, *entries) == ["Briefmarken", "Kopierpapier", "Miete Juli"]

    # Partner: Alpha vor Zebra, Buchungen ohne Partner am Ende – in beiden Richtungen.
    html = client.get(f"{url}&sort=partner&dir=asc").get_data(as_text=True)
    assert _order(html, *entries) == ["Miete Juli", "Kopierpapier", "Briefmarken"]
    html = client.get(f"{url}&sort=partner&dir=desc").get_data(as_text=True)
    assert _order(html, *entries) == ["Kopierpapier", "Miete Juli", "Briefmarken"]

    # Kontierung nach Soll-Konto: 4210 vor 4930.
    html = client.get(f"{url}&sort=posting&dir=asc").get_data(as_text=True)
    assert _order(html, *entries)[0] == "Miete Juli"

    html = client.get(f"{url}&sort=number&dir=desc").get_data(as_text=True)
    assert _order(html, *entries) == ["Briefmarken", "Miete Juli", "Kopierpapier"]


def test_journal_sort_survives_paging_and_filters(setup):
    _, client, ids = setup
    url = f"/buchungen?company_id={ids['company_id']}&sort=amount&dir=desc&limit=2"
    html = client.get(url).get_data(as_text=True)
    assert "Miete Juli" in html and "Kopierpapier" in html and "Briefmarken" not in html
    assert "sort=amount" in html and "dir=desc" in html and "offset=2" in html
    assert '<input type="hidden" name="sort" value="amount" />' in html

    html = client.get(f"{url}&offset=2").get_data(as_text=True)
    assert "Briefmarken" in html and "Miete Juli" not in html


def test_unknown_sort_column_falls_back_to_default(setup):
    _, client, ids = setup
    response = client.get(f"/buchungen?company_id={ids['company_id']}&sort=drop&dir=desc")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-sort="date" data-sort-dir="desc"' in html
    assert '<input type="hidden" name="sort"' not in html


def test_bank_sorts_by_amount_and_counterparty(setup):
    _, client, ids = setup
    url = f"/bank?company_id={ids['company_id']}"
    rows = ("Umsatz Mittel", "Umsatz Klein", "Umsatz Gross")
    html = client.get(f"{url}&sort=amount&dir=asc").get_data(as_text=True)
    assert _order(html, *rows) == ["Umsatz Gross", "Umsatz Mittel", "Umsatz Klein"]
    html = client.get(f"{url}&sort=counterparty&dir=asc").get_data(as_text=True)
    assert _order(html, *rows) == ["Umsatz Gross", "Umsatz Klein", "Umsatz Mittel"]
    html = client.get(f"{url}&sort=account").get_data(as_text=True)
    assert 'data-sort="account" data-sort-dir="asc"' in html


def test_partners_sort_by_city_role_and_entry_count(setup):
    _, client, ids = setup
    url = f"/partner?company_id={ids['company_id']}"
    html = client.get(url).get_data(as_text=True)
    assert _order(html, "Alpha Miete", "Zebra Papier") == ["Alpha Miete", "Zebra Papier"]
    html = client.get(f"{url}&sort=city&dir=asc").get_data(as_text=True)
    assert _order(html, "Alpha Miete", "Zebra Papier") == ["Zebra Papier", "Alpha Miete"]
    html = client.get(f"{url}&sort=role&dir=desc").get_data(as_text=True)
    assert _order(html, "Alpha Miete", "Zebra Papier") == ["Zebra Papier", "Alpha Miete"]
    html = client.get(f"{url}&sort=entries&dir=desc").get_data(as_text=True)
    assert _order(html, "Alpha Miete", "Zebra Papier") == ["Alpha Miete", "Zebra Papier"]


def test_documents_sort_by_name_and_date(setup):
    _, client, ids = setup
    url = f"/belege?company_id={ids['company_id']}"
    names = ("b-rechnung.pdf", "c-quittung.pdf", "a-vertrag.pdf")
    html = client.get(url).get_data(as_text=True)
    assert _order(html, *names) == ["b-rechnung.pdf", "a-vertrag.pdf", "c-quittung.pdf"]
    html = client.get(f"{url}&sort=name&dir=asc").get_data(as_text=True)
    assert _order(html, *names) == ["a-vertrag.pdf", "b-rechnung.pdf", "c-quittung.pdf"]
    html = client.get(f"{url}&sort=date&dir=asc").get_data(as_text=True)
    assert _order(html, *names) == ["c-quittung.pdf", "a-vertrag.pdf", "b-rechnung.pdf"]


def test_open_items_sort_by_amount_and_due_date(setup):
    _, client, ids = setup
    url = f"/offene-posten?company_id={ids['company_id']}"
    refs = ("OP-Mitte", "OP-Gross", "OP-Klein")
    html = client.get(url).get_data(as_text=True)
    assert _order(html, *refs) == ["OP-Gross", "OP-Mitte", "OP-Klein"]
    html = client.get(f"{url}&sort=open&dir=desc").get_data(as_text=True)
    assert _order(html, *refs) == ["OP-Gross", "OP-Mitte", "OP-Klein"]
    html = client.get(f"{url}&sort=open&dir=asc").get_data(as_text=True)
    assert _order(html, *refs) == ["OP-Klein", "OP-Mitte", "OP-Gross"]
    html = client.get(f"{url}&sort=due&dir=desc").get_data(as_text=True)
    assert _order(html, *refs) == ["OP-Klein", "OP-Mitte", "OP-Gross"]


def test_account_ledger_sort_keeps_chronological_running_balance(setup):
    app, client, ids = setup
    with app.extensions["db_session_factory"]() as session:
        bank = session.get(Account, ids["bank_id"])
        chronological = account_ledger(session=session, account=bank)
        balance_by_entry = {line["description"]: line["balance"] for line in chronological["lines"]}
        assert balance_by_entry["Briefmarken"] == Decimal("-935.00")

        by_credit = account_ledger(session=session, account=bank, sort="credit", descending=True)
        assert [line["description"] for line in by_credit["lines"]] == [
            "Miete Juli",
            "Kopierpapier",
            "Briefmarken",
        ]
        assert {
            line["description"]: line["balance"] for line in by_credit["lines"]
        } == balance_by_entry

        paged = account_ledger(
            session=session, account=bank, sort="balance", descending=False, limit=1, offset=0
        )
        assert [line["description"] for line in paged["lines"]] == ["Briefmarken"]
        assert paged["total_lines"] == 3

        office = session.get(Account, ids["office_id"])
        by_text = account_ledger(session=session, account=office, sort="text")
        assert [line["description"] for line in by_text["lines"]] == [
            "Briefmarken",
            "Kopierpapier",
        ]
        by_contra = account_ledger(session=session, account=office, sort="contra")
        assert [line["contra_accounts"][0]["code"] for line in by_contra["lines"]] == [
            "1200",
            "1200",
        ]

        with pytest.raises(AccountLedgerError):
            account_ledger(session=session, account=bank, sort="unbekannt")

    html = client.get(
        f"/konten/{ids['bank_id']}?fiscal_year_id=alle&sort=credit&dir=desc"
    ).get_data(as_text=True)
    assert 'data-sort="credit" data-sort-dir="desc"' in html
    assert _order(html, "Miete Juli", "Kopierpapier", "Briefmarken") == [
        "Miete Juli",
        "Kopierpapier",
        "Briefmarken",
    ]


def test_account_ledger_api_and_mcp_accept_sort(setup):
    _, client, ids = setup
    url = f"/api/v1/accounts/{ids['bank_id']}/ledger"
    response = client.get(url, query_string={"sort": "credit", "dir": "desc"})
    assert response.status_code == 200
    assert [line["description"] for line in response.get_json()["lines"]] == [
        "Miete Juli",
        "Kopierpapier",
        "Briefmarken",
    ]
    assert client.get(url, query_string={"sort": "kaputt"}).status_code == 400
    assert client.get(url, query_string={"sort": "debit", "dir": "auf"}).status_code == 400

    schema = TOOLS_BY_NAME["get_account_ledger"].input_schema
    assert "balance" in schema["properties"]["sort"]["enum"]
    assert schema["properties"]["dir"]["enum"] == ["asc", "desc"]


def test_client_side_sorting_is_wired_for_other_tables():
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text()
    assert "setupTableSort" in script
    assert "data-sort-server" in script
    templates = Path(__file__).resolve().parents[1] / "app" / "templates"
    # Eingabetabelle und gerechnete Aufstellungen bleiben unsortiert.
    assert 'id="journal-lines" data-no-sort' in (templates / "buchungen.html").read_text()
    assert "<table data-no-sort>" in (templates / "ertragsteuern.html").read_text()
