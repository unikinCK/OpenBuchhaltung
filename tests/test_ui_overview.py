"""Übersichtliche Oberfläche: Journal mit Geschäftspartner, Detailseite, Formate."""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pytest
from document_files import pdf_document_bytes
from sqlalchemy import select

from app import create_app
from app.auth import hash_password
from app.services.journal_entries import JournalEntryCreationError
from app.web.formatting import (
    format_amount,
    format_date,
    format_datetime,
    format_number,
    format_rate,
)
from app.web.helpers import parse_form_amount
from domain.models import AuditLog, BankTransaction, JournalEntry, JournalEntryLine, User

BANK_CSV = """Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag
05.07.2026;OPENAI *API;OpenAI OpCo LLC;-119,00
"""


def test_display_formats() -> None:
    assert format_amount(Decimal("1234.5")) == "1.234,50"
    assert format_amount("-10656.25") == "-10.656,25"
    assert format_amount(None) == ""
    assert format_date(date(2026, 9, 30)) == "30.09.2026"
    assert format_date("2026-09-30") == "30.09.2026"
    assert format_datetime(datetime(2026, 10, 9, 9, 51)) == "09.10.2026 11:51"
    assert format_rate(Decimal("19.00")) == "19"
    assert format_rate(Decimal("10.70")) == "10,7"
    assert format_number(Decimal("100000.00")) == "100.000"
    assert format_number("1234.50") == "1.234,5"
    assert format_number(None) == ""


def test_datetimes_are_shown_in_german_local_time() -> None:
    # Sommerzeit (MESZ, UTC+2): naive Werte aus SQLite gelten als UTC.
    assert format_datetime(datetime(2026, 7, 3, 15, 42)) == "03.07.2026 17:42"
    assert format_datetime(datetime(2026, 7, 3, 15, 42, tzinfo=timezone.utc)) == "03.07.2026 17:42"
    # Winterzeit (MEZ, UTC+1).
    assert format_datetime(datetime(2026, 1, 15, 15, 42)) == "15.01.2026 16:42"
    # Zeitumstellung am 25.10.2026: 00:30 UTC = 02:30 MESZ, 01:30 UTC = 02:30 MEZ.
    assert format_datetime(datetime(2026, 10, 25, 0, 30)) == "25.10.2026 02:30"
    assert format_datetime(datetime(2026, 10, 25, 1, 30)) == "25.10.2026 02:30"
    # Aware Werte anderer Zonen werden umgerechnet, ISO-Strings (API/OAuth) ebenso.
    new_york = timezone(timedelta(hours=-4))
    assert format_datetime(datetime(2026, 7, 3, 11, 42, tzinfo=new_york)) == "03.07.2026 17:42"
    assert format_datetime("2026-12-31T23:30:00+00:00") == "01.01.2027 00:30"
    assert format_datetime(datetime(2026, 7, 3, 15, 42, 7), seconds=True) == "03.07.2026 17:42:07"
    # Reine Datumswerte bleiben unverändert.
    assert format_datetime(date(2026, 7, 3)) == "03.07.2026"
    assert format_datetime("2026-07-03") == "03.07.2026"
    assert format_datetime(None) == ""
    # Auch als reines Datum zählt der Kalendertag der Ortszeit.
    assert format_date(datetime(2026, 12, 31, 23, 30, tzinfo=timezone.utc)) == "01.01.2027"
    assert format_date("2026-12-31") == "31.12.2026"


def test_form_amounts_accept_german_and_technical_format() -> None:
    assert parse_form_amount("1.190,00") == Decimal("1190.00")
    assert parse_form_amount("11,9") == Decimal("11.90")
    assert parse_form_amount("1190.00") == Decimal("1190.00")
    assert parse_form_amount(" 7,50 € ") == Decimal("7.50")
    with pytest.raises(JournalEntryCreationError):
        parse_form_amount("1,234")


def _app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'ui.db'}"}
    )
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
    return app, client


def _setup(client) -> dict:
    """Gesellschaft mit SKR03-Konten, Steuercodes und zwei Partnern."""
    tenant = client.post(
        "/api/v1/tenants", json={"tenant_name": "UI", "company_name": "UI GmbH"}
    ).get_json()
    company_id = tenant["company"]["id"]
    assert (
        client.post(
            "/api/v1/account-chart/import", json={"company_id": company_id, "chart": "skr03"}
        ).status_code
        == 201
    )
    client.post("/api/v1/tax-codes/defaults", json={"company_id": company_id})
    accounts = {
        account["code"]: account["id"]
        for account in client.get(
            "/api/v1/accounts", query_string={"company_id": company_id}
        ).get_json()["accounts"]
    }
    supplier = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "OpenAI OpCo, LLC", "is_supplier": True},
    ).get_json()
    customer = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "Kunde Nord GmbH", "is_customer": True},
    ).get_json()
    return {
        "company_id": company_id,
        "accounts": accounts,
        "supplier": supplier,
        "customer": customer,
    }


def _book(client, company_id, description, lines, **extra) -> dict:
    response = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-05",
            "description": description,
            "lines": lines,
            **extra,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_journal_shows_partner_column_and_filters(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    supplier = data["supplier"]
    direct = _book(
        client,
        company_id,
        "OpenAI API Juli",
        [
            {"account_code": "4900", "debit_amount": "100.00"},
            {"account_code": "1200", "credit_amount": "100.00"},
        ],
        partner_id=supplier["id"],
    )
    without = _book(
        client,
        company_id,
        "Kontoführung Juli",
        [
            {"account_code": "4970", "debit_amount": "9.90"},
            {"account_code": "1200", "credit_amount": "9.90"},
        ],
    )
    finalized = client.post(f"/api/v1/journal-entries/{direct['id']}/finalize")
    assert finalized.status_code == 200

    page = client.get(f"/buchungen?company_id={company_id}").get_data(as_text=True)
    assert f'<span class="partner-number">{supplier["creditor_number"]}</span>' in page
    assert "OpenAI OpCo, LLC" in page
    # Offene Buchung ohne Partner: Schnellzuordnung aus der Liste.
    assert f'data-assign-partner="/buchungen/{without["id"]}/ergaenzen"' in page
    assert f'data-assign-partner="/buchungen/{direct["id"]}/ergaenzen"' not in page
    assert "100,00" in page and "9,90" in page

    by_partner = client.get(
        f"/buchungen?company_id={company_id}&partner_id={supplier['id']}"
    ).get_data(as_text=True)
    assert "OpenAI API Juli" in by_partner and "Kontoführung Juli" not in by_partner
    open_only = client.get(f"/buchungen?company_id={company_id}&status=offen").get_data(
        as_text=True
    )
    assert "Kontoführung Juli" in open_only and "OpenAI API Juli" not in open_only
    finalized_only = client.get(
        f"/buchungen?company_id={company_id}&status=festgeschrieben"
    ).get_data(as_text=True)
    assert "OpenAI API Juli" in finalized_only and "Kontoführung Juli" not in finalized_only

    dashboard = client.get(f"/?company_id={company_id}").get_data(as_text=True)
    assert "OpenAI OpCo, LLC" in dashboard and "Letzte Buchungen" in dashboard

    partner_page = client.get(f"/partner/{supplier['id']}").get_data(as_text=True)
    assert "OpenAI API Juli" in partner_page and "Kontoführung Juli" not in partner_page
    partner_list = client.get(f"/partner?company_id={company_id}").get_data(as_text=True)
    assert f"partner_id={supplier['id']}" in partner_list


def test_quick_partner_assignment_and_safe_return(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    supplier = data["supplier"]
    vst19 = next(
        code
        for code in client.get(
            "/api/v1/tax-codes", query_string={"company_id": company_id}
        ).get_json()["tax_codes"]
        if code["code"] == "VSt19"
    )
    entry = _book(
        client,
        company_id,
        "Kartenzahlung ohne Partner",
        [
            {"account_code": "4900", "debit_amount": "100.00", "tax_code_id": vst19["id"]},
            {"account_code": "1200", "credit_amount": "119.00"},
        ],
    )

    response = client.post(
        f"/buchungen/{entry['id']}/ergaenzen",
        data={
            "partner_id": str(supplier["id"]),
            "next": f"/buchungen?company_id={company_id}&offset=0",
        },
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/buchungen?company_id={company_id}&offset=0")
    with app.extensions["db_session_factory"]() as session:
        lines = (
            session.execute(
                select(JournalEntryLine)
                .where(JournalEntryLine.journal_entry_id == entry["id"])
                .order_by(JournalEntryLine.line_number)
            )
            .scalars()
            .all()
        )
        # Aufwand und Bank tragen den Partner, die Vorsteuerzeile nicht.
        assert [line.partner_id for line in lines] == [supplier["id"], None, supplier["id"]]
        assert session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "journal_entry", AuditLog.action == "amended"
            )
        ).scalar_one()

    # Fremde Rücksprungziele werden ignoriert (kein Open Redirect).
    for target in ("https://evil.example/buchungen", "//evil.example/", "/auth/login"):
        response = client.post(
            f"/buchungen/{entry['id']}/ergaenzen",
            data={"partner_id": str(supplier["id"]), "next": target},
        )
        assert response.headers["Location"].endswith(f"/buchungen/{entry['id']}")

    detail = client.get(f"/buchungen/{entry['id']}").get_data(as_text=True)
    assert 'name="line_partner_1"' in detail and 'name="line_partner_3"' in detail
    assert 'name="line_partner_2"' not in detail and "Steuerzeile" in detail
    assert "Festschreiben" in detail and "Stornieren" in detail
    assert "119,00" in detail


def test_journal_form_accepts_german_amounts_and_entry_partner(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    accounts = data["accounts"]
    supplier = data["supplier"]
    response = client.post(
        "/journal-entries",
        data={
            "company_id": str(company_id),
            "entry_date": "2026-07-06",
            "description": "Wartung Server",
            "partner_id": str(supplier["id"]),
            "line_account_id": [str(accounts["4900"]), str(accounts["1600"]), ""],
            "line_side": ["debit", "credit", "debit"],
            "line_amount": ["1.190,00", "1.190,00", ""],
            "line_partner_id": ["", "", ""],
        },
    )
    assert response.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(
            select(JournalEntry).where(JournalEntry.description == "Wartung Server")
        ).scalar_one()
        assert [line.debit_amount + line.credit_amount for line in entry.lines] == [
            Decimal("1190.00"),
            Decimal("1190.00"),
        ]
        assert {line.partner_id for line in entry.lines} == {supplier["id"]}

    # Rolle auf dem Sammelkonto passt nicht: Fehler, Maske bleibt geöffnet.
    rejected = client.post(
        "/journal-entries",
        data={
            "company_id": str(company_id),
            "entry_date": "2026-07-06",
            "description": "Falsche Rolle",
            "partner_id": str(data["customer"]["id"]),
            "line_account_id": [str(accounts["4900"]), str(accounts["1600"])],
            "line_side": ["debit", "credit"],
            "line_amount": ["10,00", "10,00"],
        },
        follow_redirects=True,
    )
    page = rejected.get_data(as_text=True)
    assert "ist kein Lieferant" in page
    assert 'id="neue-buchung" open' in page


def test_bank_booking_dialog_sets_partner(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    accounts = data["accounts"]
    supplier = data["supplier"]
    imported = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": company_id,
            "bank_account_id": accounts["1200"],
            "file_name": "umsaetze.csv",
            "mime_type": "text/csv",
            "content_base64": base64.b64encode(BANK_CSV.encode("utf-8")).decode("ascii"),
        },
    )
    assert imported.status_code == 201
    with app.extensions["db_session_factory"]() as session:
        transaction_id = session.execute(select(BankTransaction.id)).scalar_one()

    page = client.get(f"/bank?company_id={company_id}").get_data(as_text=True)
    assert f'popovertarget="tx-panel-{transaction_id}"' in page
    assert 'name="partner_id"' in page

    booked = client.post(
        f"/bank/{transaction_id}/buchen",
        data={
            "company_id": str(company_id),
            "contra_account_id": str(accounts["4900"]),
            "partner_id": str(supplier["id"]),
            "description": "OpenAI API Juli",
        },
        follow_redirects=True,
    )
    assert "Bankumsatz wurde verbucht (Buchung" in booked.get_data(as_text=True)
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(
            select(JournalEntry).where(JournalEntry.description == "OpenAI API Juli")
        ).scalar_one()
        assert {line.partner_id for line in entry.lines} == {supplier["id"]}
        entry_id = entry.id
    journal = client.get(f"/buchungen?company_id={company_id}").get_data(as_text=True)
    assert "OpenAI OpCo, LLC" in journal
    bank_page = client.get(f"/bank?company_id={company_id}").get_data(as_text=True)
    assert f"/buchungen/{entry_id}" in bank_page


def test_documents_filter_without_booking(tmp_path: Path) -> None:
    app, client = _app(tmp_path)
    data = _setup(client)
    company_id = data["company_id"]
    entry = _book(
        client,
        company_id,
        "Miete Juli",
        [
            {"account_code": "4200", "debit_amount": "500.00"},
            {"account_code": "1200", "credit_amount": "500.00"},
        ],
    )
    for name, entry_id in (("verknuepft.pdf", str(entry["id"])), ("lose.pdf", "")):
        response = client.post(
            "/documents",
            data={
                "company_id": str(company_id),
                "journal_entry_id": entry_id,
                "document_date": "2026-07-05",
                "document_file": (BytesIO(pdf_document_bytes(name.encode())), name),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302

    page = client.get(f"/belege?company_id={company_id}").get_data(as_text=True)
    assert "verknuepft.pdf" in page and "lose.pdf" in page
    assert "1 Beleg ohne Buchung" in page
    unlinked = client.get(f"/belege?company_id={company_id}&unlinked=1").get_data(as_text=True)
    assert "lose.pdf" in unlinked and "verknuepft.pdf" not in unlinked
