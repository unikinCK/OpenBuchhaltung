"""SKR04-Kontenrahmen: gebündelte CSV, kontenrahmensichere Funktionskonten und
Kontenrahmen-Prüfung für Altbestände des fehlerhaften SKR04-Imports — bei
SKR04- wie bei SKR03-Buchhaltung."""

from __future__ import annotations

import csv
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.account_chart_check import (
    FOREIGN_SKR04_ACCOUNTS,
    LEGACY_SKR04_ACCOUNTS,
    check_account_chart,
)
from app.services.account_chart_import import (
    BUNDLED_ACCOUNT_CHART_FILES,
    import_bundled_account_chart,
)
from app.services.bank_import import find_geldtransit_account
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.opening_balance import find_carryforward_account
from app.services.periods import _find_retained_earnings_account, close_fiscal_year
from app.services.standard_accounts import (
    BANK,
    VERBINDLICHKEITEN_LUL,
    find_standard_account,
    pick_standard_account,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    Base,
    Company,
    FiscalYear,
    JournalEntryLine,
    TaxCode,
    Tenant,
    User,
)

CORE_ACCOUNT_TYPES = {"asset", "liability", "equity", "income", "expense"}

# Funktionskonten der gebündelten Kontenrahmen (DATEV-Kontenrahmen 2026).
EXPECTED_FUNCTION_ACCOUNTS = {
    "skr03": {
        "geldtransit": "1360",
        "gewinnvortrag": "0860",
        "bank": "1200",
        "verbindlichkeiten": "1600",
        "USt19": "1776",
        "USt7": "1771",
        "VSt19": "1576",
        "VSt7": "1571",
    },
    "skr04": {
        "geldtransit": "1460",
        "gewinnvortrag": "2970",
        "bank": "1800",
        "verbindlichkeiten": "3300",
        "USt19": "3806",
        "USt7": "3801",
        "VSt19": "1406",
        "VSt7": "1401",
    },
}

# Kontenstand nach einem Import der bis Oktober 2026 ausgelieferten skr04.csv —
# inklusive „4240,Gas, Strom, Wasser,expense“, das wegen des Kommas als Konto
# „Gas“ mit der Kontoart „Strom“ ankam (der heutige Import lehnt die Zeile ab).
LEGACY_SKR04_IMPORT = (
    ("1000", "Kasse", "asset"),
    ("1200", "Bank", "asset"),
    ("1360", "Geldtransit", "asset"),
    ("1400", "Forderungen aus Lieferungen und Leistungen", "asset"),
    ("1406", "Abziehbare Vorsteuer 19 %", "asset"),
    ("1401", "Abziehbare Vorsteuer 7 %", "asset"),
    ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability"),
    ("3806", "Umsatzsteuer 19 %", "liability"),
    ("3801", "Umsatzsteuer 7 %", "liability"),
    ("2100", "Privatentnahmen", "equity"),
    ("2180", "Privateinlagen", "equity"),
    ("0400", "Immaterielle Vermögensgegenstände", "asset"),
    ("0420", "Technische Anlagen und Maschinen", "asset"),
    ("3000", "Rohstoffe und Hilfsstoffe", "expense"),
    ("3200", "Wareneingang", "expense"),
    ("3400", "Fremdleistungen", "expense"),
    ("4120", "Löhne und Gehälter", "expense"),
    ("4130", "Gesetzliche soziale Aufwendungen", "expense"),
    ("4210", "Miete", "expense"),
    ("4240", "Gas", "Strom"),
    ("4360", "Versicherungen", "expense"),
    ("4600", "Werbekosten", "expense"),
    ("4800", "Instandhaltung betrieblicher Räume", "expense"),
    ("4900", "Sonstige betriebliche Aufwendungen", "expense"),
    ("4970", "Nebenkosten des Geldverkehrs", "expense"),
    ("6200", "Abschreibungen auf Sachanlagen", "expense"),
    ("8000", "Umsatzerlöse 19 % USt", "income"),
    ("8300", "Umsatzerlöse 7 % USt", "income"),
    ("2970", "Gewinnvortrag vor Verwendung", "equity"),
)

# Gesellschaft, die nach dem fehlerhaften Import faktisch SKR03 bucht (Muster: unikin
# GmbH, Oktober 2026): 4240 bereits repariert, alle eigenen Konten mit SKR03-Nummern.
SKR03_AFTER_LEGACY_IMPORT = tuple(
    ("4240", "Gas, Strom, Wasser", "expense") if code == "4240" else (code, name, account_type)
    for code, name, account_type in LEGACY_SKR04_IMPORT
) + (
    ("0410", "Büro- und Geschäftsausstattung", "asset"),
    ("0800", "Gezeichnetes Kapital", "equity"),
    ("0840", "Kapitalrücklage", "equity"),
    ("0956", "Gewerbesteuerrückstellung", "liability"),
    ("0963", "Körperschaftsteuerrückstellung", "liability"),
    ("0970", "Sonstige Rückstellungen", "liability"),
    ("1210", "ING Girokonto", "asset"),
    ("1545", "Umsatzsteuerforderungen", "asset"),
    ("1574", "Abziehbare Vorsteuer aus innergemeinschaftlichem Erwerb 19 %", "asset"),
    ("1774", "Umsatzsteuer aus innergemeinschaftlichem Erwerb 19 %", "liability"),
    ("1790", "Umsatzsteuer Vorjahr", "liability"),
    ("2650", "Sonstige Zinsen und ähnliche Erträge", "income"),
    ("4380", "Beiträge (Kammern, Verbände)", "expense"),
    ("4650", "Bewirtungskosten (abziehbar 70 %)", "expense"),
    ("4660", "Reisekosten Arbeitnehmer", "expense"),
    ("4806", "EDV-Kosten (Software, Cloud- und KI-Dienste)", "expense"),
    ("4930", "Bürobedarf", "expense"),
    ("4950", "Rechts- und Beratungskosten", "expense"),
    ("4985", "Werkzeuge und Kleingeräte", "expense"),
    ("8125", "Steuerfreie innergemeinschaftliche Lieferungen § 4 Nr. 1b UStG", "income"),
    ("8336", "Erlöse aus im anderen EU-Land steuerpflichtigen sonstigen Leistungen", "income"),
)

# Buchungen (Soll, Haben, Betrag) wie bei der unikin GmbH: eigene SKR03-Konten, aber
# auch Vorsteuer, Umsatzsteuer, Gewinnvortrag, AfA und Erlöse aus dem alten Import.
SKR03_BOOKINGS = (
    ("1200", "0800", "25000.00"),
    ("1200", "0840", "759.90"),
    ("4950", "0970", "1000.00"),
    ("1200", "2970", "1335.83"),
    ("4806", "1200", "500.00"),
    ("4806", "1200", "250.00"),
    ("4806", "1200", "156.69"),
    ("1406", "1200", "172.16"),
    ("1401", "1200", "1.92"),
    ("4985", "1200", "2315.44"),
    ("1400", "8336", "20000.00"),
    ("1400", "8336", "20761.67"),
    ("1400", "8000", "6315.13"),
    ("1400", "3806", "1199.87"),
    ("6200", "0410", "3978.78"),
    ("1210", "2650", "158.25"),
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _company(session: Session, name: str = "Kontenrahmen GmbH") -> Company:
    tenant = Tenant(name=f"{name} Mandant")
    company = Company(tenant=tenant, name=name, currency_code="EUR")
    session.add_all([tenant, company])
    session.commit()
    return company


def _add_accounts(session: Session, company: Company, rows) -> dict[str, Account]:
    accounts = {}
    for code, name, account_type in rows:
        account = Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
        )
        session.add(account)
        accounts[code] = account
    session.commit()
    return accounts


def _accounts_by_code(session: Session, company: Company) -> dict[str, Account]:
    return {
        account.code: account
        for account in session.execute(
            select(Account).where(Account.company_id == company.id)
        ).scalars()
    }


def _book(
    session: Session,
    company: Company,
    entry_date: date,
    debit: Account,
    credit: Account,
    amount: str = "100.00",
):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Testbuchung",
            status="posted",
            lines=[
                JournalLineInput(debit.id, Decimal(amount), Decimal("0.00")),
                JournalLineInput(credit.id, Decimal("0.00"), Decimal(amount)),
            ],
        ),
    )


# --- gebündelte CSV-Dateien --------------------------------------------------


@pytest.mark.parametrize("chart", sorted(BUNDLED_ACCOUNT_CHART_FILES))
def test_bundled_chart_files_are_well_formed(chart: str) -> None:
    with BUNDLED_ACCOUNT_CHART_FILES[chart].open(encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))

    assert rows[0] == ["code", "name", "account_type"]
    # Genau drei Spalten je Zeile: Bezeichnungen mit Komma stehen in Anführungszeichen.
    assert all(len(row) == 3 for row in rows), [row for row in rows if len(row) != 3]
    codes = [row[0] for row in rows[1:]]
    assert all(re.fullmatch(r"\d{4}", code) for code in codes)
    assert len(codes) == len(set(codes))
    assert {row[2] for row in rows[1:]} <= CORE_ACCOUNT_TYPES


def test_bundled_skr04_has_datev_numbers(session: Session) -> None:
    company = _company(session)
    report = import_bundled_account_chart(session=session, company_id=company.id, chart="skr04")

    assert report.error_rows == 0
    assert report.imported_rows == report.total_rows == 30
    accounts = _accounts_by_code(session, company)
    expected = {
        "1600": ("Kasse", "asset"),
        "1800": ("Bank", "asset"),
        "1460": ("Geldtransit", "asset"),
        "1200": ("Forderungen aus Lieferungen und Leistungen", "asset"),
        "1406": ("Abziehbare Vorsteuer 19 %", "asset"),
        "1401": ("Abziehbare Vorsteuer 7 %", "asset"),
        "3300": ("Verbindlichkeiten aus Lieferungen und Leistungen", "liability"),
        "3806": ("Umsatzsteuer 19 %", "liability"),
        "3801": ("Umsatzsteuer 7 %", "liability"),
        "2100": ("Privatentnahmen allgemein", "equity"),
        "2180": ("Privateinlagen", "equity"),
        "2970": ("Gewinnvortrag vor Verwendung", "equity"),
        "4400": ("Erlöse 19 % USt", "income"),
        "4300": ("Erlöse 7 % USt", "income"),
        "4830": ("Sonstige betriebliche Erträge", "income"),
        "5900": ("Fremdleistungen", "expense"),
        "6000": ("Löhne und Gehälter", "expense"),
        "6325": ("Gas, Strom, Wasser", "expense"),
        "6855": ("Nebenkosten des Geldverkehrs", "expense"),
    }
    assert {
        code: (accounts[code].name, accounts[code].account_type) for code in expected
    } == expected
    # Keine SKR03-Nummern mehr (alter Fehler: Kasse 1000, Bank 1200 …, Erlöse 8000/8300).
    assert not {"1000", "1360", "1400", "1776", "1576", "8000", "8300", "8400"} & set(accounts)


@pytest.mark.parametrize("chart", sorted(EXPECTED_FUNCTION_ACCOUNTS))
def test_function_accounts_resolve_for_bundled_chart(session: Session, chart: str) -> None:
    company = _company(session)
    import_bundled_account_chart(session=session, company_id=company.id, chart=chart)
    expected = EXPECTED_FUNCTION_ACCOUNTS[chart]

    assert find_geldtransit_account(session=session, company_id=company.id).code == (
        expected["geldtransit"]
    )
    assert _find_retained_earnings_account(session, company.id).code == expected["gewinnvortrag"]
    assert find_standard_account(
        session=session, company_id=company.id, standard=BANK
    ).code == expected["bank"]
    assert find_standard_account(
        session=session, company_id=company.id, standard=VERBINDLICHKEITEN_LUL
    ).code == expected["verbindlichkeiten"]

    ensure_default_tax_codes(session=session, company=company)
    vat_accounts = {
        tax_code.code: session.get(Account, tax_code.vat_account_id).code
        for tax_code in session.execute(
            select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code != "frei")
        ).scalars()
    }
    assert vat_accounts == {
        code: expected[code] for code in ("USt19", "USt7", "VSt19", "VSt7")
    }


# --- Funktionskonten: Nummern mit anderer Bedeutung im anderen Kontenrahmen -----


def test_geldtransit_is_not_confused_with_skr04_darlehen(session: Session) -> None:
    company = _company(session)
    accounts = _add_accounts(session, company, [("1360", "Darlehen", "asset")])
    # SKR04 1360 ist „Darlehen“ — die Nummer allein ist kein Geldtransit.
    assert find_geldtransit_account(session=session, company_id=company.id) is None

    transit = _add_accounts(session, company, [("1460", "Geldtransit", "asset")])["1460"]
    found = find_geldtransit_account(session=session, company_id=company.id)
    assert found.id == transit.id != accounts["1360"].id


def test_geldtransit_is_not_confused_with_skr03_zweifelhafte_forderungen(
    session: Session,
) -> None:
    company = _company(session)
    _add_accounts(session, company, [("1460", "Zweifelhafte Forderungen", "asset")])
    assert find_geldtransit_account(session=session, company_id=company.id) is None

    _add_accounts(session, company, [("1360", "Geldtransit", "asset")])
    assert find_geldtransit_account(session=session, company_id=company.id).code == "1360"


def test_geldtransit_prefers_skr04_number_next_to_legacy_account(session: Session) -> None:
    # Altbestand (1360 aus dem fehlerhaften Import) plus korrigierter SKR04-Import.
    company = _company(session)
    _add_accounts(
        session, company, [("1360", "Geldtransit", "asset"), ("1460", "Geldtransit", "asset")]
    )
    assert find_geldtransit_account(session=session, company_id=company.id).code == "1460"


def test_retained_earnings_ignores_skr04_beteiligungen(session: Session) -> None:
    company = _company(session)
    _add_accounts(
        session, company, [("0860", "Beteiligungen an Personengesellschaften", "asset")]
    )
    # SKR04 0860 ist ein Aktivkonto — dort darf kein Jahresergebnis landen.
    assert _find_retained_earnings_account(session, company.id) is None

    _add_accounts(session, company, [("2970", "Gewinnvortrag vor Verwendung", "equity")])
    assert _find_retained_earnings_account(session, company.id).code == "2970"


def test_retained_earnings_accepts_renamed_equity_account(session: Session) -> None:
    company = _company(session)
    _add_accounts(session, company, [("0860", "Ergebnisvortrag", "equity")])
    assert _find_retained_earnings_account(session, company.id).code == "0860"


def test_close_fiscal_year_books_result_to_skr04_retained_earnings(session: Session) -> None:
    company = _company(session)
    import_bundled_account_chart(session=session, company_id=company.id, chart="skr04")
    accounts = _accounts_by_code(session, company)
    # Ein SKR04-Finanzanlagekonto 0860 darf den Vortrag nicht abfangen.
    _add_accounts(
        session, company, [("0860", "Beteiligungen an Personengesellschaften", "asset")]
    )
    _book(session, company, date(2026, 5, 4), accounts["1800"], accounts["4400"])
    fiscal_year = session.execute(select(FiscalYear)).scalar_one()

    result = close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")

    lines = {
        session.get(Account, line.account_id).code: (line.debit_amount, line.credit_amount)
        for line in session.execute(
            select(JournalEntryLine).where(
                JournalEntryLine.journal_entry_id == result.carryforward_entry.id
            )
        ).scalars()
    }
    assert lines == {
        "4400": (Decimal("100.00"), Decimal("0.00")),
        "2970": (Decimal("0.00"), Decimal("100.00")),
    }


def test_carryforward_account_prefers_9000(session: Session) -> None:
    company = _company(session)
    _add_accounts(
        session,
        company,
        [
            ("0999", "Saldenvortrag Altsystem", "equity"),
            ("9009", "Saldenvorträge, Kreditoren", "equity"),
            ("9000", "Saldenvorträge, Sachkonten", "equity"),
        ],
    )
    assert find_carryforward_account(session=session, company_id=company.id).code == "9000"


def test_default_bank_and_creditor_accounts_follow_the_chart(session: Session) -> None:
    company = _company(session)
    skr04 = _add_accounts(
        session,
        company,
        [
            ("1200", "Forderungen aus Lieferungen und Leistungen", "asset"),
            ("1600", "Kasse", "asset"),
            ("1800", "Bank", "asset"),
            ("3300", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability"),
        ],
    )
    accounts = list(skr04.values())
    assert pick_standard_account(accounts, BANK).code == "1800"
    assert pick_standard_account(accounts, VERBINDLICHKEITEN_LUL).code == "3300"

    other = _company(session, "SKR03 GmbH")
    skr03 = _add_accounts(
        session,
        other,
        [
            ("1200", "Bank", "asset"),
            ("1400", "Forderungen aus Lieferungen und Leistungen", "asset"),
            ("1600", "Verbindlichkeiten", "liability"),
            ("1800", "Privatentnahmen allgemein", "equity"),
        ],
    )
    accounts = list(skr03.values())
    assert pick_standard_account(accounts, BANK).code == "1200"
    # Kurzbezeichnung ohne „aus Lieferungen“: Nummer 1600 mit Kontoart liability genügt.
    assert pick_standard_account(accounts, VERBINDLICHKEITEN_LUL).code == "1600"


# --- Kontenrahmen-Prüfung -----------------------------------------------------


def test_check_reports_legacy_skr04_accounts(session: Session) -> None:
    company = _company(session)
    accounts = _add_accounts(session, company, LEGACY_SKR04_IMPORT)
    _book(session, company, date(2026, 3, 2), accounts["1000"], accounts["8000"])

    result = check_account_chart(session=session, company_id=company.id)

    assert result["ok"] is False
    assert result["skr04_markers"] == ["1401", "1406", "3801", "3806", "2970"]
    # Nur der alte Import, keine eigenen Konten: Es gilt der importierte SKR04.
    assert result["dominant_chart"] == "skr04"
    assert result["chart_evidence"] == {
        "skr03": {"accounts": 0, "posting_count": 0},
        "skr04": {"accounts": 0, "posting_count": 0},
    }
    assert result["foreign_skr04_accounts"] == []
    assert result["nonstandard_skr03_accounts"] == []
    rows = {row["code"]: row for row in result["legacy_skr04_accounts"]}
    assert set(rows) == {legacy.code for legacy in LEGACY_SKR04_ACCOUNTS}
    # 0420 „Technische Anlagen und Maschinen“ liegt im SKR04 richtig (0400er-Gruppe).
    assert "0420" not in rows
    kasse = rows["1000"]
    assert (kasse["skr04_code"], kasse["skr04_name"]) == ("1600", "Kasse")
    assert (kasse["posting_count"], kasse["balance"]) == (1, "100.00")
    # Die SKR04-Nummer der Kasse ist durch das Altkonto „Verbindlichkeiten …“ belegt.
    assert kasse["skr04_status"] == "occupied"
    assert kasse["skr04_account"]["name"] == "Verbindlichkeiten aus Lieferungen und Leistungen"
    assert (rows["1200"]["skr04_status"], rows["1200"]["skr04_account"]) == ("missing", None)
    assert rows["8000"]["balance"] == "-100.00"
    assert [row["code"] for row in result["unknown_account_types"]] == ["4240"]
    assert result["unknown_account_types"][0]["account_type"] == "Strom"
    assert len(result["warnings"]) == 2


def test_check_drops_settled_legacy_accounts(session: Session) -> None:
    company = _company(session)
    accounts = _add_accounts(session, company, LEGACY_SKR04_IMPORT)
    _book(session, company, date(2026, 3, 2), accounts["1000"], accounts["8000"])
    accounts["1200"].is_active = False  # ohne Buchungen: erledigt
    accounts["1000"].is_active = False  # Saldo 100 noch nicht umgebucht
    session.commit()

    codes = {
        row["code"]
        for row in check_account_chart(session=session, company_id=company.id)[
            "legacy_skr04_accounts"
        ]
    }
    assert "1200" not in codes
    assert "1000" in codes


def test_check_marks_skr04_accounts_added_by_reimport(session: Session) -> None:
    company = _company(session)
    _add_accounts(session, company, LEGACY_SKR04_IMPORT)
    report = import_bundled_account_chart(session=session, company_id=company.id, chart="skr04")
    # Belegte Nummern (u. a. 1200 Bank, 1600 Verbindlichkeiten) werden übersprungen.
    assert (report.imported_rows, report.duplicate_rows) == (21, 9)

    result = check_account_chart(session=session, company_id=company.id)
    assert result["dominant_chart"] == "skr04"
    assert result["chart_evidence"]["skr04"] == {"accounts": 18, "posting_count": 0}
    rows = {row["code"]: row for row in result["legacy_skr04_accounts"]}
    assert (rows["1200"]["skr04_status"], rows["1200"]["skr04_account"]["name"]) == (
        "present",
        "Bank",
    )
    assert rows["1000"]["skr04_status"] == "occupied"
    assert rows["4210"]["skr04_status"] == "missing"  # 6310 ist nicht im Kontenrahmen
    # Funktionskonten greifen nach dem Re-Import auf die SKR04-Nummern zu.
    assert find_geldtransit_account(session=session, company_id=company.id).code == "1460"
    assert find_standard_account(
        session=session, company_id=company.id, standard=BANK
    ).code == "1800"
    assert find_standard_account(
        session=session, company_id=company.id, standard=VERBINDLICHKEITEN_LUL
    ).code == "3300"


@pytest.mark.parametrize("chart", sorted(BUNDLED_ACCOUNT_CHART_FILES))
def test_check_accepts_bundled_charts(session: Session, chart: str) -> None:
    company = _company(session)
    import_bundled_account_chart(session=session, company_id=company.id, chart=chart)

    result = check_account_chart(session=session, company_id=company.id)

    assert result["ok"] is True
    assert result["legacy_skr04_accounts"] == []
    assert result["foreign_skr04_accounts"] == []
    assert result["nonstandard_skr03_accounts"] == []
    assert result["unknown_account_types"] == []
    # SKR03 enthält 1000 Kasse/1200 Bank regulär — ohne SKR04-Merkmale kein Befund.
    assert bool(result["skr04_markers"]) is (chart == "skr04")
    assert result["dominant_chart"] == ("skr04" if chart == "skr04" else None)


# --- Kontenrahmen-Prüfung: SKR03-Buchhaltung nach dem alten SKR04-Import --------


def _skr03_company(
    session: Session, name: str = "SKR03 nach Altimport GmbH"
) -> tuple[Company, dict[str, Account]]:
    company = _company(session, name)
    accounts = _add_accounts(session, company, SKR03_AFTER_LEGACY_IMPORT)
    for debit, credit, amount in SKR03_BOOKINGS:
        _book(session, company, date(2026, 6, 30), accounts[debit], accounts[credit], amount)
    return company, accounts


def test_check_detects_skr03_and_reports_foreign_skr04_accounts(session: Session) -> None:
    company, _ = _skr03_company(session)

    result = check_account_chart(session=session, company_id=company.id)

    assert result["ok"] is False
    assert result["dominant_chart"] == "skr03"
    # Es zählen nur eigene Konten mit eindeutigem Bereich: 0800–0970, 1774/1790, 2650,
    # 4xxx und 8xxx. Bank-, Forderungs- und Steuerkonten der Klassen 0/1 sowie die
    # Konten des alten Imports bleiben außen vor.
    assert result["chart_evidence"] == {
        "skr03": {"accounts": 17, "posting_count": 11},
        "skr04": {"accounts": 0, "posting_count": 0},
    }
    # Die SKR03-Nummern des alten Imports sind hier richtig: keine Altkonten.
    assert result["legacy_skr04_accounts"] == []
    rows = {row["code"]: row for row in result["foreign_skr04_accounts"]}
    assert {code: row["skr03_code"] for code, row in rows.items()} == {
        "0420": "0200",
        "1401": "1571",
        "1406": "1576",
        "2100": "1800",
        "2180": "1890",
        "2970": "0860",
        "3801": "1771",
        "3806": "1776",
    }
    assert {
        code: (row["posting_count"], row["balance"])
        for code, row in rows.items()
        if row["posting_count"]
    } == {
        "1401": (1, "1.92"),
        "1406": (1, "172.16"),
        "2970": (1, "-1335.83"),
        "3806": (1, "-1199.87"),
    }
    assert (rows["2970"]["skr03_name"], rows["2970"]["skr03_status"]) == (
        "Gewinnvortrag vor Verwendung",
        "missing",
    )
    assert rows["2970"]["skr03_account"] is None
    # Was die SKR04-Nummer im SKR03 bedeutet (DATEV-Kontenrahmen SKR03 2026).
    assert rows["2100"]["skr03_meaning"] == "Zinsen und ähnliche Aufwendungen"
    assert rows["1406"]["skr03_meaning"] == "reserviert"
    assert rows["0420"]["skr03_meaning"] == "Büroeinrichtung"
    assert rows["3806"]["skr03_meaning"] is None
    assert all(row["tax_codes"] == [] for row in rows.values())

    hints = {row["code"]: row for row in result["nonstandard_skr03_accounts"]}
    assert {code: row["skr03_code"] for code, row in hints.items()} == {
        "0400": "0010",
        "3400": "3100",
        "4800": "4260",
        "6200": "4830",
        "8000": "8400",
    }
    assert (hints["6200"]["posting_count"], hints["6200"]["balance"]) == (1, "3978.78")
    assert (hints["8000"]["posting_count"], hints["8000"]["balance"]) == (1, "-6315.13")
    assert hints["8000"]["skr03_name"] == "Erlöse 19 % USt"
    assert hints["3400"]["skr03_meaning"].startswith("Wareneingang 19 % Vorsteuer")
    assert len(result["warnings"]) == 2


def test_check_follows_skr03_cleanup(session: Session) -> None:
    company, accounts = _skr03_company(session)
    added = _add_accounts(
        session,
        company,
        [
            ("0860", "Gewinnvortrag vor Verwendung", "equity"),
            ("1576", "Abziehbare Vorsteuer 19 %", "asset"),
            ("4830", "Abschreibungen auf Sachanlagen", "expense"),
        ],
    )
    # Solange 2970 aktiv ist, bucht der Jahresabschluss dorthin.
    assert _find_retained_earnings_account(session, company.id).code == "2970"
    _book(session, company, date(2026, 7, 1), accounts["2970"], added["0860"], "1335.83")
    for code in ("0420", "2100", "2180", "2970", "3801", "3400"):
        accounts[code].is_active = False
    accounts["4800"].name = "Reparaturen und Instandhaltungen von technischen Anlagen und Maschinen"
    session.commit()

    result = check_account_chart(session=session, company_id=company.id)

    assert result["dominant_chart"] == "skr03"
    rows = {row["code"]: row for row in result["foreign_skr04_accounts"]}
    # Deaktiviert und ausgeglichen gilt als erledigt — auch der umgebuchte Gewinnvortrag.
    assert set(rows) == {"1401", "1406", "3806"}
    assert (rows["1406"]["skr03_status"], rows["1406"]["skr03_account"]["code"]) == (
        "present",
        "1576",
    )
    assert rows["1401"]["skr03_status"] == "missing"
    hints = {row["code"]: row for row in result["nonstandard_skr03_accounts"]}
    # 3400 ohne Buchungen deaktiviert, 4800 in die DATEV-Bezeichnung umbenannt.
    assert set(hints) == {"0400", "6200", "8000"}
    assert hints["6200"]["skr03_status"] == "present"
    assert _find_retained_earnings_account(session, company.id).code == "0860"


def test_check_stays_skr03_after_skr04_reimport(session: Session) -> None:
    # Wer der früheren Empfehlung folgte und den korrigierten SKR04 nachimportierte,
    # bucht weiter SKR03: Unbebuchte SKR04-Konten überstimmen das nicht.
    company, _ = _skr03_company(session)
    report = import_bundled_account_chart(session=session, company_id=company.id, chart="skr04")
    assert report.imported_rows == 21

    result = check_account_chart(session=session, company_id=company.id)

    assert result["dominant_chart"] == "skr03"
    assert result["chart_evidence"]["skr04"] == {"accounts": 18, "posting_count": 0}
    assert result["legacy_skr04_accounts"] == []
    rows = {row["code"]: row for row in result["foreign_skr04_accounts"]}
    # SKR04 1800 „Bank“ belegt die SKR03-Nummer der Privatentnahmen …
    assert rows["2100"]["skr03_status"] == "occupied"
    assert rows["2100"]["skr03_account"]["name"] == "Bank"
    # … und SKR04 4830 „Sonstige betriebliche Erträge“ die SKR03-AfA auf Sachanlagen.
    hints = {row["code"]: row for row in result["nonstandard_skr03_accounts"]}
    assert hints["6200"]["skr03_status"] == "occupied"


def test_check_keeps_skr04_despite_single_skr03_account(session: Session) -> None:
    # Ein versehentlich mit SKR03-Nummer angelegtes Konto kippt keinen SKR04-Kontenplan.
    company = _company(session)
    import_bundled_account_chart(session=session, company_id=company.id, chart="skr04")
    bank = _accounts_by_code(session, company)["1800"]
    capital = _add_accounts(session, company, [("0800", "Gezeichnetes Kapital", "equity")])
    _book(session, company, date(2026, 1, 2), bank, capital["0800"], "25000.00")

    result = check_account_chart(session=session, company_id=company.id)

    assert result["dominant_chart"] == "skr04"
    assert result["chart_evidence"]["skr03"] == {"accounts": 1, "posting_count": 1}
    assert result["ok"] is True
    assert result["foreign_skr04_accounts"] == []


def test_check_warns_about_tax_codes_on_foreign_tax_accounts(session: Session) -> None:
    company, _ = _skr03_company(session)
    # Ohne SKR03-Steuerkonten verknüpfen die Standard-Steuercodes die SKR04-Nummern.
    ensure_default_tax_codes(session=session, company=company)
    session.commit()

    result = check_account_chart(session=session, company_id=company.id)

    assert {
        row["code"]: row["tax_codes"]
        for row in result["foreign_skr04_accounts"]
        if row["tax_codes"]
    } == {"1401": ["VSt7"], "1406": ["VSt19"], "3801": ["USt7"], "3806": ["USt19"]}
    assert any(warning.startswith("Steuerkonten mit Steuercode") for warning in result["warnings"])


# --- API, MCP-Pfad und UI -----------------------------------------------------


def _create_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_skr04.db'}",
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


def _app_company(app, rows=None, name: str = "Kontenrahmen GmbH") -> int:
    with app.extensions["db_session_factory"]() as db_session:
        company = _company(db_session, name)
        if rows is not None:
            _add_accounts(db_session, company, rows)
        return company.id


def test_check_api_endpoint(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    company_id = _app_company(app, LEGACY_SKR04_IMPORT)
    client = app.test_client()

    response = client.get("/api/v1/account-chart/check", query_string={"company_id": company_id})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["dominant_chart"] == "skr04"
    assert {"1000", "1200", "8000"} <= {row["code"] for row in payload["legacy_skr04_accounts"]}

    with app.extensions["db_session_factory"]() as db_session:
        skr03_id = _skr03_company(db_session)[0].id
    payload = client.get(
        "/api/v1/account-chart/check", query_string={"company_id": skr03_id}
    ).get_json()
    assert payload["dominant_chart"] == "skr03"
    assert payload["legacy_skr04_accounts"] == []
    assert {"1406", "2970", "3806"} <= {row["code"] for row in payload["foreign_skr04_accounts"]}
    assert {"6200", "8000"} <= {row["code"] for row in payload["nonstandard_skr03_accounts"]}

    assert client.get("/api/v1/account-chart/check").status_code == 400
    missing = client.get("/api/v1/account-chart/check", query_string={"company_id": 999})
    assert missing.status_code == 404


def test_accounts_page_shows_check_only_with_findings(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    legacy_id = _app_company(app, LEGACY_SKR04_IMPORT, name="Alt GmbH")
    clean_id = _app_company(app, name="Neu GmbH")
    with app.extensions["db_session_factory"]() as db_session:
        import_bundled_account_chart(session=db_session, company_id=clean_id, chart="skr04")
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    legacy_page = client.get("/konten", query_string={"company_id": legacy_id}).data.decode()
    assert "Kontenrahmen-Prüfung" in legacy_page
    assert "1000 – Kasse" in legacy_page
    assert "1600 – Kasse" in legacy_page

    clean_page = client.get("/konten", query_string={"company_id": clean_id}).data.decode()
    assert "Kontenrahmen-Prüfung" not in clean_page


def test_accounts_page_shows_skr03_findings_and_hints(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    with app.extensions["db_session_factory"]() as db_session:
        skr03_id = _skr03_company(db_session)[0].id
        # Nur Hinweise: Fremdkonten erledigt (ohne Buchungen, deaktiviert), 6200/8000 offen.
        hints_company = _company(db_session, "Hinweis GmbH")
        hints_accounts = _add_accounts(db_session, hints_company, SKR03_AFTER_LEGACY_IMPORT)
        for foreign in FOREIGN_SKR04_ACCOUNTS:
            hints_accounts[foreign.code].is_active = False
        db_session.commit()
        hints_id = hints_company.id
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    page = client.get("/konten", query_string={"company_id": skr03_id}).data.decode()
    assert "Vorherrschender Kontenrahmen: <strong>SKR03</strong>" in page
    assert "1406 – Abziehbare Vorsteuer 19 %" in page
    assert "1576 – Abziehbare Vorsteuer 19 %" in page
    assert "im SKR03: Zinsen und ähnliche Aufwendungen" in page
    assert "0860 – Gewinnvortrag vor Verwendung" in page
    assert "8400 – Erlöse 19 % USt" in page

    with app.extensions["db_session_factory"]() as db_session:
        hints_only = check_account_chart(session=db_session, company_id=hints_id)
    assert hints_only["ok"] is True
    assert hints_only["foreign_skr04_accounts"] == []
    hints_page = client.get("/konten", query_string={"company_id": hints_id}).data.decode()
    assert "Kontenrahmen-Prüfung" in hints_page
    assert "4830 – Abschreibungen auf Sachanlagen" in hints_page


def _selected_option(page: str, select_id: str) -> str:
    select = re.search(rf'<select id="{select_id}".*?</select>', page, re.S).group(0)
    return re.search(r"<option value=\"\d+\" selected>([^<]+)</option>", select).group(1)


def test_masks_preselect_skr04_bank_and_creditor(tmp_path: Path) -> None:
    app = _create_app(tmp_path)
    company_id = _app_company(app)
    with app.extensions["db_session_factory"]() as db_session:
        import_bundled_account_chart(session=db_session, company_id=company_id, chart="skr04")
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    bank_page = client.get("/bank", query_string={"company_id": company_id}).data.decode()
    # Früher fest 1200 — im SKR04 sind das die Forderungen.
    assert _selected_option(bank_page, "bank_account_id") == "1800 – Bank"

    einvoice_page = client.get("/erechnung", query_string={"company_id": company_id}).data.decode()
    # Früher fest 1600 — im SKR04 ist das die Kasse.
    assert _selected_option(einvoice_page, "creditor_account_id") == (
        "3300 – Verbindlichkeiten aus Lieferungen und Leistungen"
    )
