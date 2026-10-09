from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.account_chart_check import detect_company_chart
from app.services.datev_export import (
    DatevExportError,
    DatevExportOptions,
    DatevExportPeriod,
    build_datev_export,
    datev_account_function,
    datev_export_file_name,
    datev_tax_from_gross,
    resolve_datev_export_period,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    finalize_journal_entries_until,
    reverse_journal_entry,
)
from app.services.periods import create_fiscal_year
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    Base,
    Company,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    TaxCode,
    Tenant,
    User,
)

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


def _ledger_balances(
    session: Session, company: Company, period: DatevExportPeriod | None = None
) -> dict[str, Decimal]:
    """Salden laut Journal, mit ``period`` nur der Buchungen dieses Stapels."""
    query = (
        select(Account.code, JournalEntryLine.debit_amount, JournalEntryLine.credit_amount)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(Account.company_id == company.id)
    )
    if period is not None:
        query = query.where(
            JournalEntry.fiscal_year_id == period.fiscal_year_id,
            JournalEntry.entry_date.between(period.date_from, period.date_to),
        )
    balances: dict[str, Decimal] = defaultdict(Decimal)
    for code, debit, credit in session.execute(query):
        balances[code] += debit - credit
    return {code: balance for code, balance in balances.items() if balance}


def _header(content: str) -> list[str]:
    return content.split("\r\n")[0].split(";")


def _datev_dates(content: str) -> list[date]:
    """Belegdaten, wie DATEV sie liest: TTMM, das Jahr aus dem WJ-Beginn (Kopffeld 13).

    Das Datum liegt im Wirtschaftsjahr ab Feld 13 (bei abweichendem WJ im
    Folgejahr, wenn Tag/Monat vor dem WJ-Beginn liegen) und muss in den
    Stapelzeitraum Datum von/bis (Felder 15/16) fallen.
    """
    header = _header(content)
    fiscal_year_start = datetime.strptime(header[12], "%Y%m%d").date()
    date_from = datetime.strptime(header[14], "%Y%m%d").date()
    date_to = datetime.strptime(header[15], "%Y%m%d").date()
    dates = []
    for row in _rows(content):
        day, month = int(row[9][:2]), int(row[9][2:])
        year = fiscal_year_start.year
        if (month, day) < (fiscal_year_start.month, fiscal_year_start.day):
            year += 1
        booked = date(year, month, day)
        assert date_from <= booked <= date_to, f"{booked} außerhalb des Stapels"
        dates.append(booked)
    return dates


def _two_years(session: Session, company: Company) -> dict[str, DatevExportPeriod]:
    """Buchungen in den Wirtschaftsjahren 2025 und 2026 (Kalenderjahre)."""
    _book(session, company, "Rechnung November", ("1400", "1190.00", "0"),
          ("8400", "0", "1000.00"), ("1776", "0", "190.00"), day=date(2025, 11, 20))
    _book(session, company, "Barverkauf Silvester", ("1000", "107.00", "0"),
          ("8300", "0", "100.00"), ("1771", "0", "7.00"), day=date(2025, 12, 31))
    _book(session, company, "Bürobedarf", ("4930", "100.00", "0"),
          ("1576", "19.00", "0"), ("1200", "0", "119.00"), day=date(2026, 1, 15))
    _book(session, company, "Zahlung Rechnung November", ("1200", "1190.00", "0"),
          ("1400", "0", "1190.00"), day=date(2026, 3, 31))
    _book(session, company, "Rechnung April", ("1400", "595.00", "0"),
          ("8400", "0", "500.00"), ("1776", "0", "95.00"), day=date(2026, 4, 1))
    return {
        fiscal_year.label: resolve_datev_export_period(
            session=session, company_id=company.id, fiscal_year_id=fiscal_year.id
        )
        for fiscal_year in session.execute(
            select(FiscalYear).where(FiscalYear.company_id == company.id)
        ).scalars()
    }


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
    # Ohne Auswahl das einzige Wirtschaftsjahr mit Buchungen, ganz.
    assert header_fields[14] == "20260101"  # Datum von
    assert header_fields[15] == "20261231"  # Datum bis
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


def test_invoice_tax_off_by_a_cent_gets_a_rounding_row(session: Session) -> None:
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

    # Brutto auf dem Automatikkonto, der Cent wandert per Korrektursatz (BU 40,
    # damit 3400 darauf keine Steuer rechnet) aufs Vorsteuerkonto.
    assert _row_summary(content) == [
        ("119,01", '"H"', "1600", "3400", '""'),
        ("0,01", '"S"', "1576", "3400", '"40"'),
    ]
    assert _rows(content)[1][13] == '"Steuer-Rundungsdifferenz Wareneingang laut Rechnung"'
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_larger_tax_difference_keeps_tax_line_and_suspends_automatic(session: Session) -> None:
    company = _seed(session)
    # 19,10 € Steuer auf 100 €: 8 Cent über DATEVs 19,02 € – kein Rundungsfall mehr.
    _book(
        session,
        company,
        "Wareneingang mit abweichender Steuer",
        ("3400", "100.00", "0"),
        ("1576", "19.10", "0"),
        ("1600", "0", "119.10"),
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("100,00", '"H"', "1600", "3400", '"40"'),
        ("19,10", '"S"', "1576", "1600", '""'),
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


def test_split_with_rounding_difference_gets_a_rounding_row(session: Session) -> None:
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

    assert _row_summary(content) == [
        ("1,02", '"S"', "1200", "8400", '""'),
        ("1,02", '"S"', "1000", "8400", '""'),
        ("116,96", '"S"', "1360", "8400", '""'),
        ("0,01", '"H"', "1776", "8400", '"40"'),
    ]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company)


def test_foreign_tax_accounts_are_exported_under_the_chart_number(session: Session) -> None:
    # SKR03-Buchhaltung mit Vorsteuerkonten aus dem alten SKR04-Import (wie die unikin).
    company = _seed(
        session,
        SKR03_ACCOUNTS
        + (
            ("1401", "Abziehbare Vorsteuer 7 %", "asset"),
            ("1406", "Abziehbare Vorsteuer 19 %", "asset"),
            ("4650", "Bewirtungskosten", "expense"),
            ("4654", "Nicht abziehbare Bewirtungskosten", "expense"),
        ),
    )
    # Bewirtung mit 7 % und 19 %, aufgeteilt nach Abziehbarkeit: kein Steuerpaar.
    _book(
        session,
        company,
        "Bewirtung",
        ("4650", "24.46", "0"),
        ("4654", "10.48", "0"),
        ("1401", "1.92", "0"),
        ("1406", "1.44", "0"),
        ("1200", "0", "38.30"),
    )
    # Bereinigung: Saldo von 1406 auf 1576 umbuchen – im SKR03 dasselbe Konto.
    _book(session, company, "Umbuchung Vorsteuer", ("1576", "1.44", "0"), ("1406", "0", "1.44"))

    content = _export(session, company)

    assert _row_summary(content) == [
        ("24,46", '"S"', "4650", "1200", '""'),
        ("10,48", '"S"', "4654", "1200", '""'),
        ("1,92", '"S"', "1571", "1200", '""'),
        ("1,44", '"S"', "1576", "1200", '""'),
    ]
    ledger = _ledger_balances(session, company)
    ledger["1571"] = ledger.pop("1401")
    ledger["1576"] = ledger.pop("1576") + ledger.pop("1406", Decimal("0"))
    assert _datev_balances(content, "skr03") == ledger


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


def test_old_skr04_tax_accounts_without_tax_codes(session: Session) -> None:
    # SKR03-Buchhaltung ohne Steuercodes mit Steuerkonten aus dem alten SKR04-Import
    # (wie die unikin GmbH): dieselbe Steuerkonten-Erkennung wie in der UStVA.
    company = _seed(
        session,
        SKR03_ACCOUNTS
        + (
            ("1406", "Abziehbare Vorsteuer 19 %", "asset"),
            ("3806", "Umsatzsteuer 19 %", "liability"),
        ),
    )
    _book(
        session,
        company,
        "Erlös, USt auf 3806",
        ("1400", "1190.00", "0"),
        ("8000", "0", "1000.00"),
        ("3806", "0", "190.00"),
    )
    _book(
        session,
        company,
        "Bürobedarf, VSt auf 1406",
        ("4930", "100.00", "0"),
        ("1406", "19.00", "0"),
        ("1200", "0", "119.00"),
    )

    content = _export(session, company)

    assert _row_summary(content) == [
        ("1190,00", '"S"', "1400", "8000", '"101"'),
        ("119,00", '"H"', "1200", "4930", '"401"'),
    ]
    # DATEV bucht die Steuer auf seine SKR03-Steuerkonten statt auf 3806/1406.
    ledger = _ledger_balances(session, company)
    ledger["1776"] = ledger.pop("3806")
    ledger["1576"] = ledger.pop("1406")
    assert _datev_balances(content, "skr03") == ledger


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


def test_export_contains_one_fiscal_year(session: Session) -> None:
    company = _seed(session)
    periods = _two_years(session, company)

    content = _export(session, company, period=periods["2025"])

    # Kopf: WJ-Beginn und Stapelzeitraum des Wirtschaftsjahres 2025.
    assert _header(content)[12:16] == ["20250101", "4", "20250101", "20251231"]
    assert _row_summary(content) == [
        ("1190,00", '"S"', "1400", "8400", '""'),
        ("107,00", '"S"', "1000", "8300", '""'),
    ]
    # DATEV liest TTMM im Jahr ab WJ-Beginn: Belegdaten wie im Journal.
    assert _datev_dates(content) == [date(2025, 11, 20), date(2025, 12, 31)]
    assert _datev_balances(content, "skr03") == _ledger_balances(
        session, company, periods["2025"]
    )

    content = _export(session, company, period=periods["2026"])

    assert _header(content)[12:16] == ["20260101", "4", "20260101", "20261231"]
    assert _datev_dates(content) == [date(2026, 1, 15), date(2026, 3, 31), date(2026, 4, 1)]
    assert _datev_balances(content, "skr03") == _ledger_balances(
        session, company, periods["2026"]
    )


def test_without_selection_several_fiscal_years_must_be_chosen(session: Session) -> None:
    company = _seed(session)
    _two_years(session, company)

    with pytest.raises(DatevExportError, match="mehreren Wirtschaftsjahren") as excinfo:
        resolve_datev_export_period(session=session, company_id=company.id)
    # Zur Auswahl die Wirtschaftsjahre mit Buchungen, das jüngste zuerst.
    assert [(year.label, year.entry_count) for year in excinfo.value.fiscal_years] == [
        ("2026", 3),
        ("2025", 2),
    ]
    with pytest.raises(DatevExportError):
        _export(session, company)

    # Ein Zeitraum bestimmt das Wirtschaftsjahr.
    period = resolve_datev_export_period(
        session=session, company_id=company.id, date_from=date(2025, 12, 1)
    )
    assert (period.label, period.date_from, period.date_to) == (
        "2025",
        date(2025, 12, 1),
        date(2025, 12, 31),
    )
    period = resolve_datev_export_period(
        session=session, company_id=company.id, date_to=date(2026, 2, 28)
    )
    assert (period.label, period.date_from, period.date_to) == (
        "2026",
        date(2026, 1, 1),
        date(2026, 2, 28),
    )


def test_period_within_fiscal_year(session: Session) -> None:
    company = _seed(session)
    periods = _two_years(session, company)
    fiscal_year_id = periods["2026"].fiscal_year_id

    period = resolve_datev_export_period(
        session=session,
        company_id=company.id,
        fiscal_year_id=fiscal_year_id,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 3, 31),
    )
    content = _export(session, company, period=period)

    # WJ-Beginn bleibt Feld 13, der Zeitraum steht in Datum von/bis.
    assert _header(content)[12:16] == ["20260101", "4", "20260101", "20260331"]
    assert _datev_dates(content) == [date(2026, 1, 15), date(2026, 3, 31)]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company, period)


@pytest.mark.parametrize(
    ("selection", "message"),
    [
        # Über die Grenze des Wirtschaftsjahres: DATEV läse 2026er Belege als 2025.
        (
            {"date_from": date(2025, 12, 1), "date_to": date(2026, 1, 31)},
            "nicht im Wirtschaftsjahr 2025",
        ),
        ({"year": "2025", "date_from": date(2026, 1, 1)}, "nicht im Wirtschaftsjahr 2025"),
        ({"year": "2026", "date_to": date(2025, 12, 31)}, "nicht im Wirtschaftsjahr 2026"),
        ({"date_from": date(2026, 3, 1), "date_to": date(2026, 2, 1)}, "liegt nach dem Datum bis"),
        ({"date_from": date(2024, 6, 1)}, "kein Wirtschaftsjahr angelegt"),
        ({"fiscal_year_id": 999}, "Wirtschaftsjahr nicht gefunden"),
    ],
)
def test_invalid_selection_is_rejected(session: Session, selection, message) -> None:
    company = _seed(session)
    periods = _two_years(session, company)
    if "year" in selection:
        selection = dict(selection)
        selection["fiscal_year_id"] = periods[selection.pop("year")].fiscal_year_id

    with pytest.raises(DatevExportError, match=message):
        resolve_datev_export_period(session=session, company_id=company.id, **selection)


def test_fiscal_year_of_another_company_is_not_found(session: Session) -> None:
    company = _seed(session)
    periods = _two_years(session, company)
    other = Company(tenant_id=company.tenant_id, name="Andere GmbH", currency_code="EUR")
    session.add(other)
    session.commit()

    with pytest.raises(DatevExportError, match="Wirtschaftsjahr nicht gefunden"):
        resolve_datev_export_period(
            session=session, company_id=other.id, fiscal_year_id=periods["2025"].fiscal_year_id
        )


def test_deviating_fiscal_year_starts_header_at_its_begin(session: Session) -> None:
    company = _seed(session)
    company.fiscal_year_start_month = 7
    session.commit()
    _book(session, company, "Ende WJ 2025/2026", ("1200", "50.00", "0"),
          ("8000", "0", "50.00"), day=date(2026, 6, 30))
    _book(session, company, "Anfang WJ 2026/2027", ("1400", "1190.00", "0"),
          ("8400", "0", "1000.00"), ("1776", "0", "190.00"), day=date(2026, 7, 1))
    _book(session, company, "März im Folgejahr", ("1200", "1190.00", "0"),
          ("1400", "0", "1190.00"), day=date(2027, 3, 15))
    fiscal_year = session.execute(
        select(FiscalYear).where(FiscalYear.label == "2026/2027")
    ).scalar_one()

    period = resolve_datev_export_period(
        session=session, company_id=company.id, fiscal_year_id=fiscal_year.id
    )
    content = _export(session, company, period=period)

    assert _header(content)[12:16] == ["20260701", "4", "20260701", "20270630"]
    assert [row[9] for row in _rows(content)] == ["0107", "1503"]
    # 15.03. liegt vor dem WJ-Beginn 01.07. und gehört damit ins Jahr 2027.
    assert _datev_dates(content) == [date(2026, 7, 1), date(2027, 3, 15)]
    assert _datev_balances(content, "skr03") == _ledger_balances(session, company, period)
    assert (
        datev_export_file_name(company.id, period)
        == f"EXTF_Buchungsstapel_{company.id}_WJ2026-2027_20260701-20270630.csv"
    )


def test_short_fiscal_year_starts_header_at_its_begin(session: Session) -> None:
    company = _seed(session)
    create_fiscal_year(
        session=session,
        company_id=company.id,
        label="2026 (Rumpf)",
        start_date=date(2026, 4, 15),
        end_date=date(2026, 12, 31),
        changed_by="pytest",
    )
    _book(session, company, "Erste Rechnung", ("1400", "1190.00", "0"),
          ("8400", "0", "1000.00"), ("1776", "0", "190.00"), day=date(2026, 4, 20))

    period = resolve_datev_export_period(session=session, company_id=company.id)
    content = _export(session, company, period=period)

    assert _header(content)[12:16] == ["20260415", "4", "20260415", "20261231"]
    assert _datev_dates(content) == [date(2026, 4, 20)]
    assert (
        datev_export_file_name(company.id, period)
        == f"EXTF_Buchungsstapel_{company.id}_WJ2026-Rumpf_20260415-20261231.csv"
    )


def test_finalization_flag_counts_only_exported_entries(session: Session) -> None:
    company = _seed(session)
    periods = _two_years(session, company)
    finalize_journal_entries_until(
        session=session, company_id=company.id, up_to_date=date(2026, 1, 31), changed_by="pytest"
    )
    january = resolve_datev_export_period(
        session=session,
        company_id=company.id,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
    )

    # Kopffeld 21: 1 schreibt den Stapel in DATEV fest (Dok.-Nr. 1080697) – nur,
    # wenn alle Buchungen dieses Stapels festgeschrieben sind.
    assert _header(_export(session, company, period=periods["2025"]))[20] == "1"
    assert _header(_export(session, company, period=january))[20] == "1"
    assert _header(_export(session, company, period=periods["2026"]))[20] == "0"


def test_company_without_fiscal_year_exports_empty_regular_year(session: Session) -> None:
    company = _seed(session)
    company.fiscal_year_start_month = 7
    session.commit()

    period = resolve_datev_export_period(
        session=session, company_id=company.id, today=date(2026, 3, 1)
    )
    content = _export(session, company, period=period)

    # Reguläres Wirtschaftsjahr zum Stichtag, leerer Stapel ohne Festschreibung.
    assert (period.fiscal_year_id, period.label) == (None, "2025/2026")
    assert _header(content)[12:16] == ["20250701", "4", "20250701", "20260630"]
    assert _header(content)[20] == "0"
    assert _rows(content) == []


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


def test_datev_export_per_fiscal_year_in_api_and_ui(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "M", "company_name": "M GmbH"})
    for code, name, account_type in (("1200", "Bank", "asset"), ("8400", "Erlöse", "income")):
        client.post(
            "/accounts",
            data={"company_id": "1", "code": code, "name": name, "account_type": account_type},
        )
    # Je eine Buchung in den Wirtschaftsjahren 2025 (ID 1) und 2026 (ID 2).
    for entry_date, amount in (("2025-11-20", "100.00"), ("2026-05-04", "250.00")):
        client.post(
            "/journal-entries",
            data={
                "company_id": "1",
                "entry_date": entry_date,
                "description": "Umsatz",
                "debit_account_id": "1",
                "credit_account_id": "2",
                "amount": amount,
            },
        )

    def export(**params):
        return client.get("/api/v1/exports/datev.csv", query_string={"company_id": 1, **params})

    # Ohne Auswahl ist das Wirtschaftsjahr Pflicht; der Fehler nennt die Auswahl.
    ambiguous = export()
    assert ambiguous.status_code == 400
    assert "mehreren Wirtschaftsjahren" in ambiguous.get_json()["error"]
    assert ambiguous.get_json()["fiscal_years"] == [
        {
            "id": 2,
            "label": "2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "entry_count": 1,
        },
        {
            "id": 1,
            "label": "2025",
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "entry_count": 1,
        },
    ]

    response = export(fiscal_year_id=1)
    assert response.status_code == 200
    assert (
        'filename="EXTF_Buchungsstapel_1_WJ2025_20250101-20251231.csv"'
        in response.headers["Content-Disposition"]
    )
    body = response.get_data().decode("cp1252")
    assert _header(body)[12:16] == ["20250101", "4", "20250101", "20251231"]
    assert [row[0] for row in _rows(body)] == ["100,00"]

    response = export(date_from="2026-05-01", date_to="2026-05-31")
    assert response.status_code == 200
    assert "WJ2026_20260501-20260531.csv" in response.headers["Content-Disposition"]
    assert [row[0] for row in _rows(response.get_data().decode("cp1252"))] == ["250,00"]

    across = export(date_from="2025-12-01", date_to="2026-01-31")
    assert across.status_code == 400
    assert "nicht im Wirtschaftsjahr 2025" in across.get_json()["error"]
    assert export(fiscal_year_id=99).get_json()["error"] == "Wirtschaftsjahr nicht gefunden."
    assert export(fiscal_year_id="abc").status_code == 400
    assert export(fiscal_year_id="²").status_code == 400  # isdigit(), aber kein int
    assert export(date_from="2026-13-01").status_code == 400

    # Berichte-Seite: Auswahl der Wirtschaftsjahre, vorgewählt das jüngste mit
    # Buchungen bzw. das des Auswertungszeitraums.
    page = client.get("/berichte", query_string={"company_id": 1}).get_data(as_text=True)
    assert 'id="datev_fiscal_year_id"' in page
    assert "2025 (2025-01-01 – 2025-12-31, 1 Buchung)" in page
    assert 'value="2" selected' in page
    page = client.get("/berichte", query_string={"company_id": 1, "date_to": "2025-12-31"})
    assert 'value="1" selected' in page.get_data(as_text=True)

    # Das Formular prüft die Auswahl und lädt über die API herunter.
    download = client.get("/reports/datev.csv", query_string={"company_id": 1, "fiscal_year_id": 1})
    assert download.status_code == 302
    location = urlsplit(download.headers["Location"])
    assert location.path == "/api/v1/exports/datev.csv"
    assert parse_qs(location.query) == {"company_id": ["1"], "fiscal_year_id": ["1"]}
    download = client.get(
        "/reports/datev.csv",
        query_string={"company_id": 1, "date_from": "2026-05-01"},
        follow_redirects=True,
    )
    assert "WJ2026_20260501-20261231.csv" in download.headers["Content-Disposition"]

    # Ein Zeitraum außerhalb des Wirtschaftsjahres kommt als Hinweis zurück,
    # die Eingaben bleiben stehen.
    rejected = client.get(
        "/reports/datev.csv",
        query_string={"company_id": 1, "fiscal_year_id": 1, "date_from": "2026-01-01"},
        follow_redirects=True,
    )
    page = rejected.get_data(as_text=True)
    assert rejected.request.path == "/berichte"
    assert "DATEV-Export: Der gewählte Zeitraum (ab 2026-01-01) liegt nicht im" in page
    assert 'value="1" selected' in page
    assert 'name="date_from" type="date" value="2026-01-01"' in page
