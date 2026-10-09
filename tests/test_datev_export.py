from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.account_chart_check import detect_company_chart
from app.services.datev_export import (
    DatevExportOptions,
    build_datev_export,
    datev_account_function,
    datev_tax_from_gross,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import Account, Base, Company, JournalEntryLine, TaxCode, Tenant, User

GENERATED_AT = datetime(2026, 7, 6, 14, 30, 0, tzinfo=timezone.utc)

# Ausschnitt aus dem DATEV-Kontenrahmen SKR03 2026 (Art.-Nr. 11174): 8400/8300 und
# 3400 sind Automatikkonten (AM/AV), 8125 steuerfrei mit Automatik, 8000 und 4930
# Konten ohne Automatik (Zusatzfunktion M bzw. V).
SKR03_ACCOUNTS = (
    ("0860", "Gewinnvortrag vor Verwendung", "equity"),
    ("1000", "Kasse", "asset"),
    ("1200", "Bank", "asset"),
    ("1360", "Geldtransit", "asset"),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset"),
    ("1571", "Abziehbare Vorsteuer 7 %", "asset"),
    ("1576", "Abziehbare Vorsteuer 19 %", "asset"),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability"),
    ("1771", "Umsatzsteuer 7 %", "liability"),
    ("1776", "Umsatzsteuer 19 %", "liability"),
    ("3400", "Wareneingang 19 % Vorsteuer", "expense"),
    ("4930", "Bürobedarf", "expense"),
    ("8000", "Umsatzerlöse", "income"),
    ("8125", "Steuerfreie innergemeinschaftliche Lieferungen", "income"),
    ("8300", "Erlöse 7 % USt", "income"),
    ("8400", "Erlöse 19 % USt", "income"),
)
SKR04_ACCOUNTS = (
    ("1200", "Forderungen aus Lieferungen und Leistungen", "asset"),
    ("1406", "Abziehbare Vorsteuer 19 %", "asset"),
    ("1800", "Bank", "asset"),
    ("3300", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability"),
    ("3806", "Umsatzsteuer 19 %", "liability"),
    ("4400", "Erlöse 19 % USt", "income"),
    ("6815", "Bürobedarf", "expense"),
)
# Steuerkonten, auf die DATEV die errechnete Steuer bucht.
DATEV_TAX_ACCOUNTS = {
    "skr03": {("AM", "19"): "1776", ("AM", "7"): "1771", ("AV", "19"): "1576", ("AV", "7"): "1571"},
    "skr04": {("AM", "19"): "3806", ("AM", "7"): "3801", ("AV", "19"): "1406", ("AV", "7"): "1401"},
}
TAX_KEY_FUNCTIONS = {
    "101": ("AM", "19"),
    "102": ("AM", "7"),
    "401": ("AV", "19"),
    "402": ("AV", "7"),
}


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session, accounts=SKR03_ACCOUNTS) -> Company:
    tenant = Tenant(name="DATEV Tenant")
    company = Company(tenant=tenant, name="DATEV GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    for code, name, atype in accounts:
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


def _book(session: Session, company: Company, description: str, *lines, day=date(2026, 4, 1)):
    """Bucht Zeilen (Konto, Soll, Haben[, Steuercode]) ohne Steuer-Expansion."""
    tax_codes = {
        tax_code.code: tax_code.id
        for tax_code in session.execute(
            select(TaxCode).where(TaxCode.company_id == company.id)
        ).scalars()
    }
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description=description,
            status="posted",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(
                    account_code=line[0],
                    debit_amount=Decimal(line[1]),
                    credit_amount=Decimal(line[2]),
                    tax_code_id=tax_codes[line[3]] if len(line) > 3 else None,
                )
                for line in lines
            ],
        ),
    )


def _export(session: Session, company: Company, **kwargs) -> str:
    return build_datev_export(
        session=session, company_id=company.id, generated_at=GENERATED_AT, **kwargs
    )


def _rows(content: str) -> list[list[str]]:
    return [line.split(";") for line in content.split("\r\n")[2:] if line]


def _row_summary(content: str) -> list[tuple[str, str, str, str, str]]:
    """(Umsatz, S/H, Konto, Gegenkonto, BU-Schlüssel) je Buchungssatz."""
    return [(row[0], row[1], row[6], row[7], row[8]) for row in _rows(content)]


def _datev_balances(content: str, chart: str) -> dict[str, Decimal]:
    """Salden (Soll − Haben), die DATEV nach dem Import bucht.

    Bildet die DATEV-Logik nach: Der Umsatz ist brutto; ein Steuerschlüssel
    (101/102/401/402) oder ein Automatikkonto mit Steuersatz rechnet die Steuer
    heraus und bucht sie auf das Steuerkonto, BU 40 hebt die Automatik auf.
    """
    balances: dict[str, Decimal] = defaultdict(Decimal)
    for row in _rows(content):
        amount = Decimal(row[0].replace(",", "."))
        account, contra, bu_key = row[6], row[7], row[8].strip('"')
        assert account and contra, "Konto und Gegenkonto sind DATEV-Muss-Felder"
        sign = 1 if row[1] == '"S"' else -1
        balances[account] += sign * amount
        balances[contra] -= sign * amount
        taxed = None
        if bu_key in TAX_KEY_FUNCTIONS:
            taxed, function = contra, TAX_KEY_FUNCTIONS[bu_key]
        elif bu_key == "":
            for code in (account, contra):
                info = datev_account_function(chart, code)
                if info.automatic and info.tax not in ("frei", "sonder"):
                    taxed, function = code, (info.automatic, info.tax)
        else:
            assert bu_key == "40", bu_key
        if taxed is None:
            continue
        # Die Steuer wandert auf derselben Seite vom gebuchten Konto aufs Steuerkonto.
        tax = datev_tax_from_gross(amount, Decimal(function[1]))
        direction = 1 if (taxed == account) == (sign == 1) else -1
        balances[taxed] -= direction * tax
        balances[DATEV_TAX_ACCOUNTS[chart][function]] += direction * tax
    return {code: balance for code, balance in balances.items() if balance}


def _ledger_balances(session: Session, company: Company) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = defaultdict(Decimal)
    for code, debit, credit in session.execute(
        select(Account.code, JournalEntryLine.debit_amount, JournalEntryLine.credit_amount)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(Account.company_id == company.id)
    ):
        balances[code] += debit - credit
    return {code: balance for code, balance in balances.items() if balance}


def test_header_and_simple_booking_row(session: Session) -> None:
    company = _seed(session)
    _book(session, company, "Barverkauf", ("1200", "119.00", "0"), ("8400", "0", "119.00"),
          day=date(2026, 3, 15))

    content = _export(
        session, company, options=DatevExportOptions(consultant_number=4711, client_number=815)
    )
    lines = content.split("\r\n")

    # Kopfzeile: 31 Felder wie im DATEV-Muster, Sachkontenrahmen in Feld 27.
    assert lines[0].startswith('"EXTF";700;21;"Buchungsstapel";13;20260706143000000')
    header_fields = lines[0].split(";")
    assert len(header_fields) == 31
    assert header_fields[10] == "4711"  # Berater
    assert header_fields[11] == "815"  # Mandant
    assert header_fields[12] == "20260101"  # WJ-Beginn
    assert header_fields[14] == "20260315"  # Datum von
    assert header_fields[15] == "20260315"  # Datum bis
    assert header_fields[26] == '"03"'  # Sachkontenrahmen SKR03

    # Spaltenüberschrift
    assert lines[1].startswith('"Umsatz (ohne Soll/Haben-Kz)";"Soll/Haben-Kennzeichen"')

    # Datenzeile: Umsatz;S/H;WKZ;...;Konto;Gegenkonto;BU;Belegdatum;Belegfeld1;...;Text
    fields = lines[2].split(";")
    assert fields[0] == "119,00"
    assert fields[1] == '"S"'
    assert fields[2] == '"EUR"'
    assert fields[6] == "1200"  # Konto (Soll)
    assert fields[7] == "8400"  # Gegenkonto (Haben)
    # 8400 rechnet in DATEV 19 % USt heraus; gebucht wurde ohne Steuer → Automatik aus.
    assert fields[8] == '"40"'
    assert fields[9] == "1503"  # Belegdatum TTMM
    assert fields[10] == '"2026-0001"'
    assert fields[13] == '"Barverkauf"'


def test_revenue_with_vat_line_is_exported_gross_on_automatic_account(session: Session) -> None:
    company = _seed(session)
    _book(
        session,
        company,
        "Ausgangsrechnung mit USt",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
    )

    content = _export(session, company)

    # Ein Satz mit Gegenkonto: DATEV rechnet die 190,00 USt aus dem Brutto heraus –
    # eine eigene Steuerzeile würde sie ein zweites Mal buchen.
    assert _row_summary(content) == [("1190,00", '"S"', "1400", "8400", '""')]
    assert _rows(content)[0][13] == '"Ausgangsrechnung mit USt"'
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_tax_codes_are_folded_per_rate(session: Session) -> None:
    company = _seed(session)
    ensure_default_tax_codes(session=session, company=company)
    tax_codes = {
        tax_code.code: tax_code.id
        for tax_code in session.execute(select(TaxCode)).scalars()
    }
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 4, 2),
            description="Rechnung 19 % und 7 %",
            status="posted",
            lines=[
                JournalLineInput(account_code="1400", debit_amount=Decimal("809.00")),
                JournalLineInput(
                    account_code="8400",
                    credit_amount=Decimal("500.00"),
                    tax_code_id=tax_codes["USt19"],
                ),
                JournalLineInput(
                    account_code="8300",
                    credit_amount=Decimal("200.00"),
                    tax_code_id=tax_codes["USt7"],
                ),
            ],
        ),
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("595,00", '"S"', "1400", "8400", '""'),
        ("214,00", '"S"', "1400", "8300", '""'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_accounts_without_automatic_get_tax_keys(session: Session) -> None:
    company = _seed(session)
    _book(
        session,
        company,
        "Erlös auf 8000",
        ("1400", "1190.00", "0"),
        ("8000", "0", "1000.00"),
        ("1776", "0", "190.00"),
    )
    _book(
        session,
        company,
        "Bürobedarf",
        ("4930", "100.00", "0"),
        ("1576", "19.00", "0"),
        ("1200", "0", "119.00"),
    )
    _book(
        session,
        company,
        "Bücher",
        ("4930", "50.00", "0"),
        ("1571", "3.50", "0"),
        ("1600", "0", "53.50"),
    )

    content = _export(session, company)

    # Steuerschlüssel 101 = USt 19 %, 401/402 = VSt 19/7 % (DATEV-Steuerschlüssel-
    # Tabelle); das Steuerkonto steht als Gegenkonto mit dem Schlüssel.
    assert _row_summary(content) == [
        ("1190,00", '"S"', "1400", "8000", '"101"'),
        ("119,00", '"H"', "1200", "4930", '"401"'),
        ("53,50", '"H"', "1600", "4930", '"402"'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_input_tax_on_automatic_account_is_exported_gross(session: Session) -> None:
    company = _seed(session)
    _book(
        session,
        company,
        "Wareneingang",
        ("3400", "100.00", "0"),
        ("1576", "19.00", "0"),
        ("1600", "0", "119.00"),
    )

    content = _export(session, company)

    assert _row_summary(content) == [("119,00", '"H"', "1600", "3400", '""')]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_invoice_tax_off_by_a_cent_keeps_tax_line_and_suspends_automatic(
    session: Session,
) -> None:
    company = _seed(session)
    # Steuer laut Rechnung 19,01 €; DATEV käme aus 119,01 € auf 19,00 €.
    _book(
        session,
        company,
        "Wareneingang laut Rechnung",
        ("3400", "100.00", "0"),
        ("1576", "19.01", "0"),
        ("1600", "0", "119.01"),
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("100,00", '"H"', "1600", "3400", '"40"'),
        ("19,01", '"S"', "1576", "1600", '""'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_rate_not_matching_the_automatic_account_falls_back(session: Session) -> None:
    company = _seed(session)
    _book(
        session,
        company,
        "7 % auf Erlöskonto 19 %",
        ("1400", "1070.00", "0"),
        ("8400", "0", "1000.00"),
        ("1771", "0", "70.00"),
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("1000,00", '"S"', "1400", "8400", '"40"'),
        ("70,00", '"S"', "1400", "1771", '""'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_automatic_accounts_without_tax_line(session: Session) -> None:
    company = _seed(session)
    _book(session, company, "Ergebnisvortrag", ("8400", "5000.00", "0"), ("0860", "0", "5000.00"))
    _book(session, company, "ig. Lieferung", ("1400", "500.00", "0"), ("8125", "0", "500.00"))

    content = _export(session, company)

    # 8400 bucht ohne Steuer → BU 40; 8125 rechnet keine Steuer, die Automatik
    # bleibt für die UStVA-Kennzahl erhalten.
    assert _row_summary(content) == [
        ("5000,00", '"H"', "0860", "8400", '"40"'),
        ("500,00", '"S"', "1400", "8125", '""'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_split_payment_keeps_gross_amount_when_tax_adds_up(session: Session) -> None:
    company = _seed(session)
    _book(
        session,
        company,
        "Barverkauf teils Karte",
        ("1200", "1000.00", "0"),
        ("1000", "190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
    )

    content = _export(session, company)

    # 159,66 € + 30,34 € = 190,00 € Umsatzsteuer.
    assert _row_summary(content) == [
        ("1000,00", '"S"', "1200", "8400", '""'),
        ("190,00", '"S"', "1000", "8400", '""'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_split_with_rounding_difference_keeps_net_and_tax_lines(session: Session) -> None:
    company = _seed(session)
    # DATEV rechnete aus 1,02 + 1,02 + 116,96 nur 0,16 + 0,16 + 18,67 = 18,99 € heraus.
    _book(
        session,
        company,
        "Verkauf auf drei Konten",
        ("1200", "1.02", "0"),
        ("1000", "1.02", "0"),
        ("1360", "116.96", "0"),
        ("8400", "0", "100.00"),
        ("1776", "0", "19.00"),
    )

    content = _export(session, company)

    rows = _row_summary(content)
    assert all(row[3] != "8400" or row[4] == '"40"' for row in rows)
    assert ("19,00", '"S"', "1360", "1776", '""') in rows
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_reversal_mirrors_the_gross_booking(session: Session) -> None:
    company = _seed(session)
    entry = _book(
        session,
        company,
        "Ausgangsrechnung",
        ("1400", "1190.00", "0"),
        ("8400", "0", "1000.00"),
        ("1776", "0", "190.00"),
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry.id,
        reversal_date=date(2026, 4, 3),
        changed_by="pytest",
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("1190,00", '"S"', "1400", "8400", '""'),
        ("1190,00", '"H"', "1400", "8400", '""'),
    ]
    assert _datev_balances(content, "skr03") == {}


def test_skr04_is_detected_and_uses_its_automatic_accounts(session: Session) -> None:
    company = _seed(session, SKR04_ACCOUNTS)
    _book(
        session,
        company,
        "Ausgangsrechnung",
        ("1200", "1190.00", "0"),
        ("4400", "0", "1000.00"),
        ("3806", "0", "190.00"),
    )
    _book(
        session,
        company,
        "Bürobedarf",
        ("6815", "100.00", "0"),
        ("1406", "19.00", "0"),
        ("1800", "0", "119.00"),
    )

    assert detect_company_chart(session=session, company_id=company.id) == "skr04"
    content = _export(session, company)

    assert content.split("\r\n")[0].split(";")[26] == '"04"'
    assert _row_summary(content) == [
        ("1190,00", '"S"', "1200", "4400", '""'),
        ("119,00", '"H"', "1800", "6815", '"401"'),
    ]
    assert _datev_balances(content, "skr04") == _ledger_balances(session, company)


def test_empty_company_produces_header_only(session: Session) -> None:
    company = _seed(session, ())
    content = _export(session, company)
    lines = [line for line in content.split("\r\n") if line]
    assert len(lines) == 2  # nur Kopf- und Überschriftzeile
    # Ohne Konten ist kein Kontenrahmen erkennbar.
    assert lines[0].split(";")[26] == '""'


def test_account_functions_follow_datev_chart_2026() -> None:
    # Automatikkonten laut DATEV-Kontenrahmen 2026 (Art.-Nr. 11174/11175).
    assert datev_account_function("skr03", "8400").automatic == "AM"
    assert datev_account_function("skr03", "8400").tax == "19"
    assert datev_account_function("skr03", "8300").tax == "7"
    assert datev_account_function("skr03", "3400").automatic == "AV"
    assert datev_account_function("skr03", "8850").tax == "19"  # „U A / M 8850“ im PDF
    assert datev_account_function("skr03", "8125").tax == "frei"
    assert datev_account_function("skr03", "3425").tax == "sonder"  # ig. Erwerb
    assert datev_account_function("skr04", "4400").tax == "19"
    assert datev_account_function("skr04", "4300").tax == "7"
    assert datev_account_function("skr04", "5400").automatic == "AV"
    # Im jeweils anderen Kontenrahmen keine Automatik.
    assert datev_account_function("skr03", "4400").automatic is None
    assert datev_account_function("skr04", "8400").automatic is None
    assert datev_account_function("skr03", "1200").restriction == "KU"
    assert datev_account_function("skr03", "1776").restriction == "KU"
    assert datev_account_function("skr03", "8000").restriction == "M"
    assert datev_account_function("skr03", "4930").restriction == "V"
    assert datev_account_function(None, "8400").automatic is None


def test_tax_key_respects_account_restrictions() -> None:
    function = datev_account_function("skr03", "4930")  # V: nur Vorsteuer
    assert function.allows_tax_key("input")
    assert not function.allows_tax_key("output")
    assert not datev_account_function("skr03", "1200").allows_tax_key("input")  # KU
    assert not datev_account_function("skr03", "8400").allows_tax_key("output")  # Automatik


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
    assert '250,00;"S";"EUR";;;"";1200;8400;"40"' in body

    # Die Berichte-Seite nennt den erkannten Kontenrahmen am Download.
    page = client.get("/berichte", query_string={"company_id": 1})
    assert "DATEV-Buchungsstapel (EXTF, SKR03)" in page.get_data(as_text=True)

    missing = client.get("/api/v1/exports/datev.csv")
    assert missing.status_code == 400
