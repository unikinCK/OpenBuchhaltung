"""UStVA-Kennzahlen für EU-Umsätze, ig. Erwerb und § 13b sowie die Zusammenfassende Meldung."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.partners import create_partner
from app.services.vat_returns import compute_vat_return_details
from app.services.zm import compute_zm
from domain.models import Account, Base, Company, Tenant, User

# SKR03-Konten (DATEV-Kontenrahmen 2026) mit UStVA-Funktion bzw. Steuerkonten für
# ig. Erwerb und § 13b.
ACCOUNTS = (
    ("0410", "Geschäftsausstattung", "asset", None),
    ("1200", "Bank", "asset", None),
    ("1360", "Geldtransit", "asset", None),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
    ("1574", "Abziehbare Vorsteuer aus innergemeinschaftlichem Erwerb 19 %", "asset", None),
    ("1577", "Abziehbare Vorsteuer nach § 13b UStG 19 %", "asset", None),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
    ("1774", "Umsatzsteuer aus innergemeinschaftlichem Erwerb 19 %", "liability", None),
    ("1776", "Umsatzsteuer 19 %", "liability", None),
    ("1787", "Umsatzsteuer nach § 13b UStG 19 %", "liability", None),
    ("2650", "Sonstige Zinsen und ähnliche Erträge", "income", None),
    (
        "3123",
        "Sonstige Leistungen eines im anderen EU-Land ansässigen Unternehmers 19 % VSt/USt",
        "expense",
        None,
    ),
    ("4806", "EDV-Kosten", "expense", None),
    ("8125", "Steuerfreie innergemeinschaftliche Lieferungen", "income", None),
    (
        "8336",
        "Erlöse aus im anderen EU-Land steuerpflichtigen sonstigen Leistungen",
        "income",
        None,
    ),
    ("8338", "Erlöse aus im Drittland steuerbaren Leistungen", "income", None),
    ("8400", "Erlöse 19 % USt", "income", None),
)
SEPTEMBER = (date(2026, 9, 1), date(2026, 9, 30))


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _company(session: Session) -> Company:
    tenant = Tenant(name="EU Tenant")
    company = Company(tenant=tenant, name="EU GmbH", currency_code="EUR")
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


def _partner(session: Session, company: Company, name: str, **kwargs):
    partner = create_partner(
        session=session, company=company, changed_by="pytest", name=name, **kwargs
    )
    session.commit()
    return partner


def _book(session: Session, company: Company, description: str, *lines, day=date(2026, 9, 10)):
    """Bucht Zeilen (Konto, Soll, Haben[, Partner-ID])."""
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description=description,
            status="posted",
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


def _details(session: Session, company: Company):
    return compute_vat_return_details(
        session=session, company_id=company.id, date_from=SEPTEMBER[0], date_to=SEPTEMBER[1]
    )


def _amounts(result) -> dict[str, Decimal]:
    return {row.kennziffer: row.amount for row in result.rows}


def test_tax_free_revenue_follows_the_account_function(session: Session) -> None:
    company = _company(session)
    _book(session, company, "ig. Lieferung", ("1400", "1000.00", "0"), ("8125", "0", "1000.00"))
    _book(session, company, "Beratung CZ", ("1400", "5000.50", "0"), ("8336", "0", "5000.50"))
    _book(session, company, "Beratung Schweiz", ("1200", "300.00", "0"), ("8338", "0", "300.00"))
    _book(session, company, "Zinsen", ("1200", "12.34", "0"), ("2650", "0", "12.34"))

    result = _details(session, company)
    amounts = _amounts(result)

    assert amounts["41"] == Decimal("1000")
    assert amounts["21"] == Decimal("5000")
    assert amounts["45"] == Decimal("300")
    # Zinsen: kein UStVA-Konto – nicht mehr pauschal Kz 48, sondern ein Hinweis.
    assert amounts["48"] == Decimal("0")
    assert [item["account_code"] for item in result.unassigned] == ["2650"]
    assert result.warnings[0].startswith("Nicht in der UStVA")
    assert amounts["83"] == Decimal("0.00")


def test_intra_eu_acquisition_and_reverse_charge(session: Session) -> None:
    company = _company(session)
    irish = _partner(
        session,
        company,
        "Software Ireland Ltd",
        is_supplier=True,
        country_code="IE",
        vat_id="IE6388047V",
    )
    # Ig. Erwerb eines Schreibtischs (wie unikin 2026-0102).
    _book(
        session,
        company,
        "Schreibtisch aus Österreich",
        ("0410", "1147.07", "0"),
        ("1574", "217.94", "0"),
        ("1774", "0", "217.94"),
        ("1360", "0", "1147.07"),
    )
    # § 13b: EU-Lieferant als Partner → Kz 46/47.
    _book(
        session,
        company,
        "Cloud-Abo Irland",
        ("4806", "100.00", "0"),
        ("1577", "19.00", "0"),
        ("1787", "0", "19.00"),
        ("1600", "0", "100.00", irish.id),
    )
    # § 13b auf dem DATEV-EU-Konto 3123, ohne Partner → Kz 46/47.
    _book(
        session,
        company,
        "Beratung Frankreich",
        ("3123", "50.00", "0"),
        ("1577", "9.50", "0"),
        ("1787", "0", "9.50"),
        ("1200", "0", "50.00"),
    )
    # § 13b ohne Angabe zum Lieferanten → Kz 84/85 mit Hinweis.
    unclassified = _book(
        session,
        company,
        "API-Guthaben",
        ("4806", "20.00", "0"),
        ("1577", "3.80", "0"),
        ("1787", "0", "3.80"),
        ("1200", "0", "20.00"),
    )

    result = _details(session, company)
    amounts = _amounts(result)

    assert amounts["89"] == Decimal("1147")
    assert amounts["61"] == Decimal("217.94")
    assert amounts["46"] == Decimal("150")
    assert amounts["47"] == Decimal("28.50")
    assert amounts["84"] == Decimal("20")
    assert amounts["85"] == Decimal("3.80")
    assert amounts["67"] == Decimal("32.30")
    assert amounts["USt"] == Decimal("250.24")
    assert amounts["66"] == Decimal("0.00")
    assert amounts["83"] == Decimal("0.00")
    assert any(unclassified.posting_number in warning for warning in result.warnings)


def test_mixed_invoice_with_domestic_input_tax_derives_the_reverse_charge_base(
    session: Session,
) -> None:
    company = _company(session)
    session.add(
        Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code="1576",
            name="Abziehbare Vorsteuer 19 %",
            account_type="asset",
        )
    )
    session.commit()
    # Eine Zahlung für ein EU-Abo (§ 13b) und Inlandsware mit normaler Vorsteuer.
    _book(
        session,
        company,
        "Sammelzahlung",
        ("4806", "100.00", "0"),
        ("1577", "19.00", "0"),
        ("1787", "0", "19.00"),
        ("4806", "50.00", "0"),
        ("1576", "9.50", "0"),
        ("1200", "0", "159.50"),
    )

    amounts = _amounts(_details(session, company))
    assert amounts["84"] == Decimal("100")
    assert amounts["85"] == Decimal("19.00")
    assert amounts["66"] == Decimal("9.50")


def test_reverse_charge_tax_without_base_line_derives_the_base(session: Session) -> None:
    company = _company(session)
    # Nachbuchung der § 13b-Steuer eines Quartals nur mit den Steuerzeilen.
    entry = _book(
        session,
        company,
        "§ 13b-Steuer Q3 nachgebucht",
        ("1577", "57.00", "0"),
        ("1787", "0", "57.00"),
    )

    amounts = _amounts(_details(session, company))
    assert amounts["84"] == Decimal("300")
    assert amounts["85"] == Decimal("57.00")
    assert amounts["67"] == Decimal("57.00")

    reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 9, 20),
        changed_by="pytest",
    )
    amounts = _amounts(_details(session, company))
    assert "84" not in amounts and "67" not in amounts
    assert amounts["83"] == Decimal("0.00")


def test_zm_rows_per_customer_vat_id(session: Session) -> None:
    company = _company(session)
    french = _partner(
        session,
        company,
        "Client SARL",
        is_customer=True,
        country_code="FR",
        vat_id="FR40303265045",
    )
    czech = _partner(
        session,
        company,
        "Wera Werk s.r.o.",
        is_customer=True,
        country_code="CZ",
        vat_id="CZ 46504320",
    )
    without_vat_id = _partner(session, company, "Kunde ohne USt-IdNr.", is_customer=True)
    _book(
        session,
        company,
        "Lieferung FR",
        ("1400", "1000.80", "0", french.id),
        ("8125", "0", "1000.80"),
    )
    _book(
        session,
        company,
        "Beratung CZ",
        ("1400", "5000.00", "0", czech.id),
        ("8336", "0", "5000.00"),
    )
    _book(
        session,
        company,
        "Gutschrift CZ",
        ("8336", "500.00", "0"),
        ("1400", "0", "500.00", czech.id),
    )
    _book(
        session, company, "Beratung ohne Partner", ("1200", "300.00", "0"), ("8336", "0", "300.00")
    )
    _book(
        session,
        company,
        "Lieferung ohne USt-IdNr.",
        ("1400", "200.00", "0", without_vat_id.id),
        ("8125", "0", "200.00"),
    )
    # Inländischer Umsatz mit Steuer gehört nicht in die ZM.
    _book(
        session,
        company,
        "Umsatz Inland",
        ("1400", "119.00", "0", french.id),
        ("8400", "0", "100.00"),
        ("1776", "0", "19.00"),
    )

    result = compute_zm(
        session=session, company_id=company.id, date_from=SEPTEMBER[0], date_to=SEPTEMBER[1]
    )

    assert [
        (row["country_code"], row["vat_id"], row["kind"], row["amount"], row["amount_euro"])
        for row in result.rows
    ] == [
        ("CZ", "CZ46504320", "S", "4500.00", "4500"),
        ("FR", "FR40303265045", "L", "1000.80", "1000"),
    ]
    assert sorted((item["account_code"], item["reason"]) for item in result.missing) == [
        ("8125", "Geschäftspartner ohne USt-IdNr."),
        ("8336", "kein Geschäftspartner zugeordnet"),
    ]
    assert result.warnings


def _create_app(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_zm.db'}"}
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


def test_zm_and_vat_warnings_via_api_and_ui(tmp_path) -> None:
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        company = _company(db_session)
        czech = _partner(
            db_session,
            company,
            "Wera Werk s.r.o.",
            is_customer=True,
            country_code="CZ",
            vat_id="CZ46504320",
        )
        _book(
            db_session,
            company,
            "Beratung CZ",
            ("1400", "4400.00", "0", czech.id),
            ("8336", "0", "4400.00"),
        )
        _book(db_session, company, "Zinsen", ("1200", "5.63", "0"), ("2650", "0", "5.63"))
        company_id = company.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    zm = client.get("/api/v1/zm", query_string={"company_id": company_id, "period": "2026-Q3"})
    assert zm.status_code == 200
    payload = zm.get_json()
    assert payload["rows"] == [
        {
            "country_code": "CZ",
            "vat_id": "CZ46504320",
            "kind": "S",
            "kind_label": "Sonstige Leistung",
            "partner_name": "Wera Werk s.r.o.",
            "amount": "4400.00",
            "amount_euro": "4400",
        }
    ]
    assert payload["missing"] == []
    assert client.get("/api/v1/zm", query_string={"company_id": company_id}).status_code == 400

    vat = client.get(
        "/api/v1/vat-return", query_string={"company_id": company_id, "period": "2026-Q3"}
    ).get_json()
    assert {row["kennziffer"]: row["amount"] for row in vat["kennzahlen"]}["21"] == "4400"
    assert [item["account_code"] for item in vat["unassigned"]] == ["2650"]
    assert vat["warnings"]

    page = client.get("/ustva", query_string={"company_id": company_id, "period": "2026-Q3"})
    html = page.get_data(as_text=True)
    assert "Zusammenfassende Meldung 2026-Q3" in html
    assert "CZ46504320" in html
    assert "Nicht in der UStVA" in html
