"""Leistungsdatum: Meldezeitraum in UStVA und ZM, Ergänzen offener Buchungen, Storno,
API/MCP/UI, DATEV-Felder 115/116 und Hashversion 4."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.compliance_integrity import (
    calculate_journal_entry_content_hash,
    verify_compliance_integrity,
)
from app.services.datev_export import build_datev_export
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    amend_journal_entry,
    create_journal_entry,
    finalize_journal_entry,
    reverse_journal_entry,
)
from app.services.mcp_server import TOOLS
from app.services.partners import create_partner
from app.services.periods import lock_period
from app.services.tax_period import INPUT, INVOICE, SERVICE, end_of_following_month, tax_point
from app.services.vat_returns import compute_vat_return_details
from app.services.zm import compute_zm
from domain.models import Account, AuditLog, Base, Company, JournalEntry, Tenant, User

# SKR03-Konten (DATEV-Kontenrahmen 2026) mit UStVA-Funktion bzw. Steuerkonten.
ACCOUNTS = (
    ("0410", "Geschäftsausstattung", "asset", None),
    ("1200", "Bank", "asset", None),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
    ("1574", "Abziehbare Vorsteuer aus innergemeinschaftlichem Erwerb 19 %", "asset", None),
    ("1576", "Abziehbare Vorsteuer 19 %", "asset", None),
    ("1577", "Abziehbare Vorsteuer nach § 13b UStG 19 %", "asset", None),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
    ("1774", "Umsatzsteuer aus innergemeinschaftlichem Erwerb 19 %", "liability", None),
    ("1776", "Umsatzsteuer 19 %", "liability", None),
    ("1787", "Umsatzsteuer nach § 13b UStG 19 %", "liability", None),
    (
        "3123",
        "Sonstige Leistungen eines im anderen EU-Land ansässigen Unternehmers 19 % VSt/USt",
        "expense",
        None,
    ),
    ("3125", "Leistungen eines im Ausland ansässigen Unternehmers 19 % VSt/USt", "expense", None),
    ("4806", "Wartungskosten für Hard- und Software", "expense", None),
    ("8125", "Steuerfreie innergemeinschaftliche Lieferungen", "income", None),
    (
        "8336",
        "Erlöse aus im anderen EU-Land steuerpflichtigen sonstigen Leistungen",
        "income",
        None,
    ),
    ("8400", "Erlöse 19 % USt", "income", None),
)
Q2 = (date(2026, 4, 1), date(2026, 6, 30))
Q3 = (date(2026, 7, 1), date(2026, 9, 30))
Q4 = (date(2026, 10, 1), date(2026, 12, 31))
YEAR_2026 = (date(2026, 1, 1), date(2026, 12, 31))


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _company(session: Session) -> Company:
    tenant = Tenant(name="Leistung Tenant")
    company = Company(tenant=tenant, name="Leistung GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    for code, name, account_type, subledger in ACCOUNTS:
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
                subledger=subledger,
            )
        )
    session.commit()
    return company


def _book(session, company, description, *lines, day, service_date=None):
    """Bucht Zeilen (Konto, Soll, Haben[, Partner-ID])."""
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description=description,
            status="posted",
            service_date=service_date,
            lines=[
                JournalLineInput(
                    account_code=line[0],
                    debit_amount=Decimal(line[1]),
                    credit_amount=Decimal(line[2]),
                    partner_id=line[3] if len(line) > 3 else None,
                )
                for line in lines
            ],
        ),
    )


def _amounts(session, company, period) -> dict[str, Decimal]:
    result = compute_vat_return_details(
        session=session, company_id=company.id, date_from=period[0], date_to=period[1]
    )
    return {row.kennziffer: row.amount for row in result.rows}


def test_tax_point_rules() -> None:
    assert end_of_following_month(date(2026, 12, 31)) == date(2027, 1, 31)
    assert end_of_following_month(date(2027, 1, 31)) == date(2027, 2, 28)
    assert tax_point(date(2026, 7, 1), date(2026, 6, 30), SERVICE) == date(2026, 6, 30)
    # Rechnung, spätestens Ende des Folgemonats der Leistung.
    assert tax_point(date(2026, 10, 2), date(2026, 8, 20), INVOICE) == date(2026, 9, 30)
    assert tax_point(date(2026, 7, 1), date(2026, 6, 30), INVOICE) == date(2026, 7, 1)
    # Vorsteuer: Leistung erbracht und Rechnung vorhanden.
    assert tax_point(date(2027, 1, 5), date(2026, 12, 20), INPUT) == date(2027, 1, 5)
    assert tax_point(date(2026, 6, 28), date(2026, 7, 2), INPUT) == date(2026, 7, 2)
    for rule in (SERVICE, INVOICE, INPUT):
        assert tax_point(date(2026, 7, 1), None, rule) == date(2026, 7, 1)


def test_sales_count_in_the_period_of_the_service(session: Session) -> None:
    company = _company(session)
    # Rechnung vom 1.7. für Juni (wie bei der unikin): Umsatz und USt gehören in Q2.
    _book(
        session,
        company,
        "Beratung Juni",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )
    _book(
        session,
        company,
        "Beratung CZ Juni",
        ("1400", "9418.77", "0"),
        ("8336", "0", "9418.77"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )

    q2 = _amounts(session, company, Q2)
    q3 = _amounts(session, company, Q3)
    assert q2["81"] == Decimal("1000")
    assert q2["USt"] == Decimal("190.00")
    assert q2["21"] == Decimal("9418")
    assert q3["81"] == Decimal("0")
    assert q3["USt"] == Decimal("0.00")
    assert "21" not in q3


def test_intra_eu_supply_follows_the_invoice_at_the_latest_the_following_month(
    session: Session,
) -> None:
    company = _company(session)
    french = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Client SARL",
        is_customer=True,
        country_code="FR",
        vat_id="FR40303265045",
    )
    session.commit()
    # Lieferung im August, Rechnung erst im Oktober: spätestens Ende September melden.
    _book(
        session,
        company,
        "Lieferung FR",
        ("1400", "1000.00", "0", french.id),
        ("8125", "0", "1000.00"),
        day=date(2026, 10, 2),
        service_date=date(2026, 8, 20),
    )

    assert _amounts(session, company, Q3)["41"] == Decimal("1000")
    assert "41" not in _amounts(session, company, Q4)
    zm_q3 = compute_zm(session=session, company_id=company.id, date_from=Q3[0], date_to=Q3[1])
    assert [(row["kind"], row["amount"]) for row in zm_q3.rows] == [("L", "1000.00")]
    zm_q4 = compute_zm(session=session, company_id=company.id, date_from=Q4[0], date_to=Q4[1])
    assert zm_q4.rows == []


def test_zm_reports_services_in_the_period_of_the_service(session: Session) -> None:
    company = _company(session)
    czech = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Wera Werk s.r.o.",
        is_customer=True,
        country_code="CZ",
        vat_id="CZ60751983",
    )
    session.commit()
    _book(
        session,
        company,
        "Beratung Juni",
        ("1400", "9418.77", "0", czech.id),
        ("8336", "0", "9418.77"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )

    zm_q2 = compute_zm(session=session, company_id=company.id, date_from=Q2[0], date_to=Q2[1])
    zm_q3 = compute_zm(session=session, company_id=company.id, date_from=Q3[0], date_to=Q3[1])
    assert [(row["vat_id"], row["kind"], row["amount_euro"]) for row in zm_q2.rows] == [
        ("CZ60751983", "S", "9418")
    ]
    assert zm_q3.rows == []


def test_input_tax_needs_service_and_invoice(session: Session) -> None:
    company = _company(session)
    # Leistung im Dezember, Rechnung im Januar: Vorsteuer erst im Januar.
    _book(
        session,
        company,
        "Wartung Dezember",
        ("4806", "100.00", "0"),
        ("1576", "19.00", "0"),
        ("1200", "0", "119.00"),
        day=date(2027, 1, 5),
        service_date=date(2026, 12, 20),
    )
    # Rechnung im Juni, Leistung im Juli: Vorsteuer erst im Juli.
    _book(
        session,
        company,
        "Wartung Juli",
        ("4806", "50.00", "0"),
        ("1576", "9.50", "0"),
        ("1200", "0", "59.50"),
        day=date(2026, 6, 28),
        service_date=date(2026, 7, 2),
    )

    assert _amounts(session, company, YEAR_2026)["66"] == Decimal("9.50")
    assert _amounts(session, company, Q2)["66"] == Decimal("0.00")
    assert _amounts(session, company, Q3)["66"] == Decimal("9.50")
    january = (date(2027, 1, 1), date(2027, 1, 31))
    assert _amounts(session, company, january)["66"] == Decimal("19.00")


def test_december_service_invoiced_in_january_belongs_to_the_old_year(
    session: Session,
) -> None:
    company = _company(session)
    _book(
        session,
        company,
        "Beratung Dezember",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
        day=date(2027, 1, 4),
        service_date=date(2026, 12, 31),
    )

    year = _amounts(session, company, YEAR_2026)
    assert year["81"] == Decimal("1000")
    assert year["83"] == Decimal("190.00")
    assert _amounts(session, company, (date(2027, 1, 1), date(2027, 12, 31)))["81"] == Decimal(
        "0"
    )


def test_reverse_charge_corrections_land_in_the_period_of_the_service(
    session: Session,
) -> None:
    company = _company(session)
    # § 13b eines EU-Unternehmers (3123): Steuer im Zeitraum der Leistung (Q1).
    _book(
        session,
        company,
        "Korrektur § 13b Microsoft März",
        ("3123", "5.77", "0"),
        ("4806", "0", "5.77"),
        ("1577", "1.10", "0"),
        ("1787", "0", "1.10"),
        day=date(2026, 10, 9),
        service_date=date(2026, 3, 10),
    )
    # Übriges § 13b (3125, Drittland): Rechnung, spätestens Ende des Folgemonats.
    _book(
        session,
        company,
        "Korrektur § 13b OpenAI März",
        ("3125", "17.23", "0"),
        ("4806", "0", "17.23"),
        ("1577", "3.27", "0"),
        ("1787", "0", "3.27"),
        day=date(2026, 10, 9),
        service_date=date(2026, 3, 12),
    )

    q1 = _amounts(session, company, (date(2026, 1, 1), date(2026, 3, 31)))
    q2 = _amounts(session, company, Q2)
    q4 = _amounts(session, company, Q4)
    assert (q1["46"], q1["47"], q1["67"]) == (Decimal("5"), Decimal("1.10"), Decimal("1.10"))
    assert (q2["84"], q2["85"], q2["67"]) == (Decimal("17"), Decimal("3.27"), Decimal("3.27"))
    assert "46" not in q4 and "84" not in q4
    assert q4["83"] == Decimal("0.00")


def test_reversal_copies_the_service_date_and_neutralizes_the_period(
    session: Session,
) -> None:
    company = _company(session)
    entry = _book(
        session,
        company,
        "Beratung Juni",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )
    reversal = reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 10, 9),
        changed_by="t",
    )

    assert reversal.service_date == date(2026, 6, 30)
    assert reversal.content_hash_version == 4
    assert _amounts(session, company, Q2)["81"] == Decimal("0")
    assert _amounts(session, company, Q4)["USt"] == Decimal("0.00")


def test_amend_open_entry_sets_service_date_and_partner_with_audit(session: Session) -> None:
    company = _company(session)
    customer = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Wera Werk s.r.o.",
        is_customer=True,
        country_code="CZ",
        vat_id="CZ60751983",
    )
    session.commit()
    entry = _book(
        session,
        company,
        "Beratung Juni",
        ("1400", "9418.77", "0"),
        ("8336", "0", "9418.77"),
        day=date(2026, 7, 1),
    )
    assert compute_zm(
        session=session, company_id=company.id, date_from=Q3[0], date_to=Q3[1]
    ).missing

    amended = amend_journal_entry(
        session=session,
        company_id=company.id,
        journal_entry_id=entry.id,
        changed_by="pruefer",
        service_date=date(2026, 6, 30),
        line_partners={1: customer.id},
    )

    assert amended.service_date == date(2026, 6, 30)
    assert amended.lines[0].partner_id == customer.id
    zm_q2 = compute_zm(session=session, company_id=company.id, date_from=Q2[0], date_to=Q2[1])
    assert [(row["vat_id"], row["amount_euro"]) for row in zm_q2.rows] == [("CZ60751983", "9418")]
    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "journal_entry", AuditLog.action == "amended"
        )
    ).scalar_one()
    assert audit.payload["service_date"] == {"old": None, "new": "2026-06-30"}
    assert audit.payload["partners"] == [
        {
            "line_number": 1,
            "account_code": "1400",
            "old_partner_id": None,
            "new_partner_id": customer.id,
        }
    ]

    # Ohne Änderung kein weiterer Protokolleintrag.
    amend_journal_entry(
        session=session,
        company_id=company.id,
        journal_entry_id=entry.id,
        changed_by="pruefer",
        service_date=date(2026, 6, 30),
        line_partners={1: customer.id},
    )
    assert (
        len(session.execute(select(AuditLog).where(AuditLog.action == "amended")).all()) == 1
    )

    # Partner nur auf Sammelkonten, Leistungsdatum lässt sich wieder entfernen.
    with pytest.raises(JournalEntryCreationError, match="Zeile 2"):
        amend_journal_entry(
            session=session,
            company_id=company.id,
            journal_entry_id=entry.id,
            changed_by="pruefer",
            line_partners={2: customer.id},
        )
    session.rollback()
    cleared = amend_journal_entry(
        session=session,
        company_id=company.id,
        journal_entry_id=entry.id,
        changed_by="pruefer",
        service_date=None,
    )
    assert cleared.service_date is None


def test_amend_refuses_finalized_and_locked_entries(session: Session) -> None:
    company = _company(session)
    finalized = _book(
        session,
        company,
        "Festgeschrieben",
        ("1200", "100.00", "0"),
        ("8400", "0", "100.00"),
        day=date(2026, 5, 4),
    )
    finalize_journal_entry(session=session, journal_entry_id=finalized.id, changed_by="t")
    with pytest.raises(JournalEntryCreationError, match="festgeschrieben"):
        amend_journal_entry(
            session=session,
            company_id=company.id,
            journal_entry_id=finalized.id,
            changed_by="t",
            service_date=date(2026, 4, 30),
        )
    session.rollback()

    locked = _book(
        session,
        company,
        "Gesperrte Periode",
        ("1200", "100.00", "0"),
        ("8400", "0", "100.00"),
        day=date(2026, 3, 4),
    )
    lock_period(session=session, period_id=locked.period_id, locked_by="t")
    session.commit()
    with pytest.raises(JournalEntryCreationError, match="gesperrt"):
        amend_journal_entry(
            session=session,
            company_id=company.id,
            journal_entry_id=locked.id,
            changed_by="t",
            service_date=date(2026, 2, 28),
        )
    session.rollback()
    with pytest.raises(JournalEntryCreationError, match="nicht gefunden"):
        amend_journal_entry(
            session=session,
            company_id=company.id + 1,
            journal_entry_id=locked.id,
            changed_by="t",
            service_date=date(2026, 2, 28),
        )


def test_seal_version_4_covers_the_service_date_and_version_3_stays_valid(
    session: Session,
) -> None:
    company = _company(session)
    entry = _book(
        session,
        company,
        "Beratung Juni",
        ("1200", "100.00", "0"),
        ("8400", "0", "100.00"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )
    finalized = finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="t")
    assert finalized.content_hash_version == 4
    sealed = finalized.content_hash
    finalized.service_date = date(2026, 7, 1)
    assert calculate_journal_entry_content_hash(finalized) != sealed
    finalized.service_date = date(2026, 6, 30)

    # Bestandssiegel der Version 3 (ohne Leistungsdatum) bleiben prüfbar.
    finalized.content_hash_version = 3
    finalized.content_hash = calculate_journal_entry_content_hash(finalized)
    session.commit()
    assert finalized.content_hash != sealed
    assert verify_compliance_integrity(session=session, company_id=company.id).valid is True


def test_datev_export_writes_service_date_and_tax_period(session: Session) -> None:
    company = _company(session)
    _book(
        session,
        company,
        "Beratung Juni",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
        day=date(2026, 7, 1),
        service_date=date(2026, 6, 30),
    )
    _book(
        session,
        company,
        "Lieferung August",
        ("1400", "1000.00", "0"),
        ("8125", "0", "1000.00"),
        day=date(2026, 10, 2),
        service_date=date(2026, 8, 20),
    )
    _book(
        session,
        company,
        "Ohne Leistungsdatum",
        ("1200", "50.00", "0"),
        ("1400", "0", "50.00"),
        day=date(2026, 7, 15),
    )

    content = build_datev_export(
        session=session,
        company_id=company.id,
        generated_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        chart="skr03",
    )
    header, columns, *rows = content.strip().split("\r\n")
    names = columns.split(";")
    assert len(names) == 116
    assert names[114:] == ['"Leistungsdatum"', '"Datum Zuord. Steuerperiode"']
    fields = {row.split(";")[13]: row.split(";") for row in rows}
    assert all(len(row.split(";")) == 116 for row in rows)
    assert fields['"Beratung Juni"'][114:] == ["30062026", "30062026"]
    # Ig. Lieferung: Rechnung vom 2.10., spätestens Ende September zu melden.
    assert fields['"Lieferung August"'][114:] == ["20082026", "30092026"]
    assert fields['"Ohne Leistungsdatum"'][114:] == ["", ""]


def test_datev_export_without_service_dates_keeps_14_fields(session: Session) -> None:
    company = _company(session)
    _book(
        session,
        company,
        "Einzahlung",
        ("1200", "50.00", "0"),
        ("8400", "0", "50.00"),
        day=date(2026, 7, 15),
    )
    content = build_datev_export(
        session=session,
        company_id=company.id,
        generated_at=datetime(2026, 10, 9, tzinfo=timezone.utc),
        chart="skr03",
    )
    _header, columns, *rows = content.strip().split("\r\n")
    assert len(columns.split(";")) == 14
    assert all(len(row.split(";")) == 14 for row in rows)


def _create_app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'service.db'}"}
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


def test_service_date_via_api_mcp_and_ui(tmp_path) -> None:
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        company = _company(db_session)
        customer = create_partner(
            session=db_session,
            company=company,
            changed_by="t",
            name="Wera Werk s.r.o.",
            is_customer=True,
            country_code="CZ",
            vat_id="CZ60751983",
        )
        db_session.commit()
        company_id = company.id
        partner_id = customer.id
        debtor_number = customer.debtor_number

    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    created = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-01",
            "service_date": "2026-06-30",
            "description": "Beratung Juni",
            "lines": [
                {"account_code": "1400", "debit_amount": "9418.77"},
                {"account_code": "8336", "credit_amount": "9418.77"},
            ],
        },
    )
    assert created.status_code == 201
    assert created.get_json()["service_date"] == "2026-06-30"
    entry_id = created.get_json()["id"]
    invalid = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-07-01",
            "service_date": "30.06.2026",
            "description": "Falsches Datum",
            "lines": [],
        },
    )
    assert invalid.status_code == 422

    amended = client.patch(
        f"/api/v1/journal-entries/{entry_id}",
        json={"lines": [{"line_number": 1, "partner_number": debtor_number}]},
    )
    assert amended.status_code == 200
    payload = amended.get_json()
    assert payload["service_date"] == "2026-06-30"
    assert payload["lines"][0]["partner_id"] == partner_id
    zm = client.get("/api/v1/zm", query_string={"company_id": company_id, "period": "2026-Q2"})
    assert zm.get_json()["rows"][0]["amount_euro"] == "9418"

    cleared = client.patch(f"/api/v1/journal-entries/{entry_id}", json={"service_date": None})
    assert cleared.get_json()["service_date"] is None
    wrong_line = client.patch(
        f"/api/v1/journal-entries/{entry_id}",
        json={"lines": [{"line_number": 2, "partner_id": partner_id}]},
    )
    assert wrong_line.status_code == 422
    assert client.patch("/api/v1/journal-entries/999999", json={}).status_code == 404

    tools = {tool.name: tool for tool in TOOLS}
    assert "service_date" in tools["create_journal_entry"].input_schema["properties"]
    amend_tool = tools["amend_journal_entry"]
    assert (amend_tool.http_method, amend_tool.path) == (
        "PATCH",
        "/journal-entries/{journal_entry_id}",
    )

    # UI: Leistungsdatum beim Erfassen, Anzeige im Journal, Ergänzen-Seite.
    with app.extensions["db_session_factory"]() as db_session:
        accounts = {
            account.code: account.id
            for account in db_session.execute(
                select(Account).where(Account.company_id == company_id)
            ).scalars()
        }
    response = client.post(
        "/journal-entries",
        data={
            "company_id": company_id,
            "entry_date": "2026-08-13",
            "service_date": "2026-07-31",
            "description": "Beratung Juli",
            "line_account_id": [accounts["1400"], accounts["8336"]],
            "line_side": ["debit", "credit"],
            "line_amount": ["15635.57", "15635.57"],
        },
    )
    assert response.status_code == 302
    with app.extensions["db_session_factory"]() as db_session:
        july = db_session.execute(
            select(JournalEntry).where(JournalEntry.description == "Beratung Juli")
        ).scalar_one()
        assert july.service_date == date(2026, 7, 31)
        july_id = july.id
    journal = client.get("/buchungen", query_string={"company_id": company_id})
    assert "Leistung 2026-07-31" in journal.get_data(as_text=True)

    page = client.get(f"/buchungen/{july_id}/ergaenzen")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'name="line_partner_1"' in html
    assert 'name="line_partner_2"' not in html
    saved = client.post(
        f"/buchungen/{july_id}/ergaenzen",
        data={"service_date": "2026-07-31", "line_partner_1": str(partner_id)},
    )
    assert saved.status_code == 302
    with app.extensions["db_session_factory"]() as db_session:
        july = db_session.get(JournalEntry, july_id)
        assert july.lines[0].partner_id == partner_id


def test_zm_attributes_a_sealed_reversal_to_the_customer_of_the_original(
    session: Session,
) -> None:
    company = _company(session)
    czech = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Wera Werk s.r.o.",
        is_customer=True,
        country_code="CZ",
        vat_id="CZ60751983",
    )
    session.commit()
    kept = _book(
        session,
        company,
        "Beratung Mai",
        ("1400", "5051.08", "0"),
        ("8336", "0", "5051.08"),
        day=date(2026, 6, 1),
    )
    doubled = _book(
        session,
        company,
        "Beratung Mai (doppelt)",
        ("1400", "5051.08", "0"),
        ("8336", "0", "5051.08"),
        day=date(2026, 6, 1),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=doubled.id,
        reversal_date=date(2026, 6, 1),
        changed_by="t",
    )
    # Das festgeschriebene Storno bleibt ohne Partner; die Originale werden ergänzt.
    for entry in (kept, doubled):
        amend_journal_entry(
            session=session,
            company_id=company.id,
            journal_entry_id=entry.id,
            changed_by="t",
            line_partners={1: czech.id},
        )

    result = compute_zm(session=session, company_id=company.id, date_from=Q2[0], date_to=Q2[1])
    assert [(row["vat_id"], row["kind"], row["amount"]) for row in result.rows] == [
        ("CZ60751983", "S", "5051.08")
    ]
    assert result.missing == []
    assert result.warnings == []
