"""SKR04-Kontenrahmen: gebündelte CSV, kontenrahmensichere Funktionskonten und
Kontenrahmen-Prüfung für Altbestände des fehlerhaften SKR04-Imports."""

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
from app.services.account_chart_check import LEGACY_SKR04_ACCOUNTS, check_account_chart
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


def _book(session: Session, company: Company, entry_date: date, debit: Account, credit: Account):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Testbuchung",
            status="posted",
            lines=[
                JournalLineInput(debit.id, Decimal("100.00"), Decimal("0.00")),
                JournalLineInput(credit.id, Decimal("0.00"), Decimal("100.00")),
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

    rows = {
        row["code"]: row
        for row in check_account_chart(session=session, company_id=company.id)[
            "legacy_skr04_accounts"
        ]
    }
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
    assert result["unknown_account_types"] == []
    # SKR03 enthält 1000 Kasse/1200 Bank regulär — ohne SKR04-Merkmale kein Befund.
    assert bool(result["skr04_markers"]) is (chart == "skr04")


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
    assert {"1000", "1200", "8000"} <= {row["code"] for row in payload["legacy_skr04_accounts"]}

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
