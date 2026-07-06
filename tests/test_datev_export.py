from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.datev_export import DatevExportOptions, build_datev_export
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, Base, Company, Tenant, User

GENERATED_AT = datetime(2026, 7, 6, 14, 30, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> Company:
    tenant = Tenant(name="DATEV Tenant")
    company = Company(tenant=tenant, name="DATEV GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    for code, name, atype in (
        ("1200", "Bank", "asset"),
        ("1400", "Forderungen", "asset"),
        ("1776", "Umsatzsteuer 19 %", "liability"),
        ("8400", "Erlöse 19 % USt", "income"),
    ):
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=atype,
            )
        )
    session.commit()
    return company


def _account_id(session: Session, company: Company, code: str) -> int:
    return next(
        a.id for a in session.query(Account).filter_by(company_id=company.id, code=code)
    )


def _line(session, company, code, debit, credit) -> JournalLineInput:
    return JournalLineInput(
        _account_id(session, company, code), Decimal(debit), Decimal(credit)
    )


def test_header_and_simple_booking_row(session: Session) -> None:
    company = _seed(session)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 3, 15),
            description="Barverkauf",
            status="posted",
            lines=[
                _line(session, company, "1200", "119.00", "0.00"),
                _line(session, company, "8400", "0.00", "119.00"),
            ],
        ),
    )

    content = build_datev_export(
        session=session,
        company_id=company.id,
        options=DatevExportOptions(consultant_number=4711, client_number=815),
        generated_at=GENERATED_AT,
    )
    lines = content.split("\r\n")

    # Kopfzeile
    assert lines[0].startswith('"EXTF";700;21;"Buchungsstapel";13;20260706143000000')
    header_fields = lines[0].split(";")
    assert header_fields[10] == "4711"  # Berater
    assert header_fields[11] == "815"  # Mandant
    assert header_fields[12] == "20260101"  # WJ-Beginn
    assert header_fields[14] == "20260315"  # Datum von
    assert header_fields[15] == "20260315"  # Datum bis

    # Spaltenüberschrift
    assert lines[1].startswith('"Umsatz (ohne Soll/Haben-Kz)";"Soll/Haben-Kennzeichen"')

    # Datenzeile: Umsatz;S/H;WKZ;...;Konto;Gegenkonto;...;Belegdatum;Belegfeld1;...;Text
    fields = lines[2].split(";")
    assert fields[0] == "119,00"
    assert fields[1] == '"S"'
    assert fields[2] == '"EUR"'
    assert fields[6] == "1200"  # Konto (Soll)
    assert fields[7] == "8400"  # Gegenkonto (Haben)
    assert fields[9] == "1503"  # Belegdatum TTMM
    assert fields[10] == '"2026-0001"'
    assert fields[13] == '"Barverkauf"'


def test_multiline_entry_is_exported_as_split(session: Session) -> None:
    company = _seed(session)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 1),
            description="Ausgangsrechnung mit USt",
            status="posted",
            lines=[
                _line(session, company, "1400", "1190.00", "0.00"),
                _line(session, company, "8400", "0.00", "1000.00"),
                _line(session, company, "1776", "0.00", "190.00"),
            ],
        ),
    )

    content = build_datev_export(
        session=session, company_id=company.id, generated_at=GENERATED_AT
    )
    data_lines = [line for line in content.split("\r\n")[2:] if line]

    # Dreizeilige Buchung → drei Splitzeilen, jeweils ohne Gegenkonto,
    # gruppiert über dieselbe Belegnummer.
    assert len(data_lines) == 3
    parsed = [line.split(";") for line in data_lines]
    assert [p[0] for p in parsed] == ["1190,00", "1000,00", "190,00"]
    assert [p[1] for p in parsed] == ['"S"', '"H"', '"H"']
    assert [p[6] for p in parsed] == ["1400", "8400", "1776"]
    assert all(p[7] == "" for p in parsed)  # kein Gegenkonto bei Splitbuchung
    assert all(p[10] == '"2026-0001"' for p in parsed)


def test_empty_company_produces_header_only(session: Session) -> None:
    company = _seed(session)
    content = build_datev_export(
        session=session, company_id=company.id, generated_at=GENERATED_AT
    )
    lines = [line for line in content.split("\r\n") if line]
    assert len(lines) == 2  # nur Kopf- und Überschriftzeile


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_datev.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    return app


def test_datev_export_endpoint(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "M", "company_name": "M GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "8400", "name": "Erlöse", "account_type": "income"},
    )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-05-04",
            "description": "Umsatz",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "250.00",
        },
    )

    response = client.get("/api/v1/exports/datev.csv", query_string={"company_id": 1})
    assert response.status_code == 200
    assert "windows-1252" in response.headers["Content-Type"]
    assert "EXTF_Buchungsstapel_1" in response.headers["Content-Disposition"]
    body = response.get_data().decode("cp1252")
    assert body.startswith('"EXTF";700;21;"Buchungsstapel"')
    assert "250,00" in body

    missing = client.get("/api/v1/exports/datev.csv")
    assert missing.status_code == 400
