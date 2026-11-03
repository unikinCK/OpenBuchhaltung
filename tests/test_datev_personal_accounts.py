"""DATEV-Personenkonten im Buchungsstapel, Stammdatenexport und Altdatenübernahme."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services.datev_export import (
    PARTNER_COLUMNS,
    DatevExportOptions,
    build_datev_export,
    build_datev_partner_export,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item
from app.services.partner_takeover import (
    PartnerTakeoverError,
    apply_partner_takeover,
    partner_takeover_proposals,
)
from app.services.partners import create_partner, set_partner_bank_details
from domain.models import (
    Account,
    AuditLog,
    Base,
    BusinessPartner,
    Company,
    OpenItem,
    Tenant,
    User,
)

GENERATED_AT = datetime(2026, 7, 6, 14, 30, 0, tzinfo=timezone.utc)
IBAN = "DE89370400440532013000"
OTHER_IBAN = "DE02120300000000202051"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> tuple[Company, dict[str, Account]]:
    tenant = Tenant(name="DATEV Mandant")
    company = Company(tenant=tenant, name="DATEV GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    accounts: dict[str, Account] = {}
    for code, name, account_type, subledger in (
        ("1200", "Bank", "asset", None),
        ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
        ("1600", "Verbindlichkeiten aus Lieferungen und Leistungen", "liability", "creditor"),
        ("8400", "Erlöse 19 %", "income", None),
        ("1776", "Umsatzsteuer 19 %", "liability", None),
        ("4210", "Miete", "expense", None),
    ):
        account = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code=code,
            name=name,
            account_type=account_type,
            subledger=subledger,
        )
        session.add(account)
        accounts[code] = account
    session.commit()
    return company, accounts


def _book(session, company, *lines, day=date(2026, 4, 1)):
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=day,
            description="Rechnung",
            status="posted",
            expand_tax_lines=False,
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


def _summary(content: str) -> list[tuple[str, str, str, str]]:
    rows = [line.split(";") for line in content.split("\r\n")[2:] if line]
    return [(row[0], row[1].strip('"'), row[6], row[7]) for row in rows]


def _partner_rows(content: str) -> list[list[str]]:
    # Zeile 1 ist die Kopfzeile, Zeile 2 die Überschrift.
    lines = content.split("\r\n")
    return list(csv.reader(StringIO("\r\n".join(lines[2:])), delimiter=";"))


def _field(row: list[str], number: int) -> str:
    return row[number - 1]


# ---------------------------------------------------------------------------
# Buchungsstapel
# ---------------------------------------------------------------------------


def test_collective_lines_with_partner_use_personal_account(session: Session) -> None:
    company, _ = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    supplier = create_partner(
        session=session, company=company, changed_by="t", name="Lieferant", is_supplier=True
    )
    session.commit()
    _book(
        session,
        company,
        ("1400", "119.00", "0.00", customer.id),
        ("8400", "0.00", "100.00"),
        ("1776", "0.00", "19.00"),
    )
    _book(session, company, ("4210", "50.00", "0.00"), ("1600", "0.00", "50.00", supplier.id))
    # Ohne Partner bleibt die Zeile auf dem Sachkonto.
    _book(session, company, ("1400", "10.00", "0.00"), ("8400", "0.00", "10.00"))
    # Partnerbezug auf einem Nicht-Sammelkonto bleibt Sachkontozeile.
    _book(session, company, ("4210", "5.00", "0.00", supplier.id), ("1200", "0.00", "5.00"))

    content = build_datev_export(session=session, company_id=company.id, generated_at=GENERATED_AT)
    summary = _summary(content)
    assert ("119,00", "S", customer.debtor_number, "8400") in [
        (amount, side, account, contra) for amount, side, account, contra in summary
    ]
    assert any(
        supplier.creditor_number in (account, contra) and amount == "50,00"
        for amount, _, account, contra in summary
    )
    assert any(
        "1400" in (account, contra) and amount == "10,00" for amount, _, account, contra in summary
    )
    assert any(
        "4210" in (account, contra) and amount == "5,00" for amount, _, account, contra in summary
    )
    assert all(
        supplier.creditor_number not in (account, contra)
        for amount, _, account, contra in summary
        if amount == "5,00"
    )


def test_personal_accounts_can_be_switched_off(session: Session) -> None:
    company, _ = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Kunde", is_customer=True
    )
    session.commit()
    _book(session, company, ("1400", "10.00", "0.00", customer.id), ("8400", "0.00", "10.00"))
    content = build_datev_export(
        session=session,
        company_id=company.id,
        generated_at=GENERATED_AT,
        options=DatevExportOptions(personal_accounts=False),
    )
    assert customer.debtor_number not in content
    assert ("10,00", "S", "1400", "8400") in _summary(content)


def test_two_partners_on_one_entry_stay_separate(session: Session) -> None:
    company, _ = _seed(session)
    first = create_partner(
        session=session, company=company, changed_by="t", name="Eins", is_customer=True
    )
    second = create_partner(
        session=session, company=company, changed_by="t", name="Zwei", is_customer=True
    )
    session.commit()
    _book(
        session,
        company,
        ("1200", "30.00", "0.00"),
        ("1400", "0.00", "10.00", first.id),
        ("1400", "0.00", "20.00", second.id),
    )
    summary = _summary(
        build_datev_export(session=session, company_id=company.id, generated_at=GENERATED_AT)
    )
    assert {row[0] for row in summary} == {"10,00", "20,00"}
    accounts = {value for row in summary for value in row[2:]}
    assert {first.debtor_number, second.debtor_number, "1200"} <= accounts
    assert "1400" not in accounts


def test_golden_batch_row_with_personal_account(session: Session) -> None:
    company, _ = _seed(session)
    customer = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Kunde",
        is_customer=True,
        debtor_number="10001",
    )
    session.commit()
    _book(session, company, ("1400", "10.00", "0.00", customer.id), ("1200", "0.00", "10.00"))
    content = build_datev_export(session=session, company_id=company.id, generated_at=GENERATED_AT)
    row = content.split("\r\n")[2]
    assert row == '10,00;"S";"EUR";;;"";10001;1200;"";0104;"2026-0001";"";;"Rechnung"'


# ---------------------------------------------------------------------------
# Stammdaten
# ---------------------------------------------------------------------------


def test_partner_columns_follow_format_description() -> None:
    assert len(PARTNER_COLUMNS) == 254
    expected = {
        1: "Konto",
        16: "Straße",
        45: "IBAN-Nr. 1",
        100: "Steuernummer",
        110: "Fälligkeit in Tagen (Debitor)",
        116: "Kreditoren-Ziel 3 Brutto (Tage)",
        220: "Nummer Fremdsystem",
        232: "Verknüpftes OPOS-Konto",
        245: "Status",
        254: "Letzte Frist",
    }
    for number, name in expected.items():
        assert PARTNER_COLUMNS[number - 1] == name


def test_partner_export_header_and_fields(session: Session) -> None:
    company, _ = _seed(session)
    both = create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Doppel GmbH",
        is_customer=True,
        is_supplier=True,
        street="Hauptstr. 1",
        postal_code="12345",
        city="Musterstadt",
        vat_id="DE123456789",
        tax_number="12/345/67890",
        email="info@example.org",
        phone="+49 30 123",
        contact_person="Frau Beispiel",
        payment_term_days=14,
    )
    set_partner_bank_details(
        session=session, partner=both, changed_by="t", iban=IBAN, bic="COBADEFFXXX"
    )
    create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Erika Mustermann",
        partner_kind="person",
        is_customer=True,
        country_code="GB",
        postal_code="SW1A 1AA",
        city="London",
        is_active=False,
    )
    session.commit()

    content = build_datev_partner_export(
        session=session,
        company_id=company.id,
        options=DatevExportOptions(consultant_number=29098, client_number=55003),
        generated_at=GENERATED_AT,
    )
    header = content.split("\r\n")[0].split(";")
    assert header[:5] == ['"EXTF"', "700", "16", '"Debitoren/Kreditoren"', "5"]
    assert header[10:14] == ["29098", "55003", "20260101", "4"]
    assert len(header) == 31

    rows = _partner_rows(content)
    rows = [row for row in rows if row]
    assert all(len(row) == 254 for row in rows)
    assert [_field(row, 1) for row in rows] == [
        both.debtor_number,
        "10001",
        both.creditor_number,
    ]

    debtor = rows[0]
    assert _field(debtor, 2) == "Doppel GmbH"
    assert _field(debtor, 7) == "2"
    assert _field(debtor, 9) == "DE"
    assert _field(debtor, 10) == "123456789"
    assert _field(debtor, 16) == "Hauptstr. 1"
    assert _field(debtor, 18) == "12345"
    assert _field(debtor, 19) == "Musterstadt"
    assert _field(debtor, 20) == "DE"
    assert _field(debtor, 26) == "1"
    assert _field(debtor, 29) == "+49 30 123"
    assert _field(debtor, 33) == "info@example.org"
    assert _field(debtor, 44) == "DE"
    assert _field(debtor, 45) == IBAN
    assert _field(debtor, 47) == "COBADEFFXXX"
    assert _field(debtor, 49) == "1"
    assert _field(debtor, 100) == "12/345/67890"
    assert _field(debtor, 102) == "Frau Beispiel"
    assert _field(debtor, 110) == "14"
    assert _field(debtor, 116) == ""
    assert _field(debtor, 232) == both.creditor_number
    assert _field(debtor, 245) == "0"

    creditor = rows[2]
    assert _field(creditor, 110) == ""
    assert _field(creditor, 116) == "14"
    assert _field(creditor, 232) == both.debtor_number

    person = rows[1]
    assert _field(person, 2) == ""
    assert _field(person, 4) == "Mustermann"
    assert _field(person, 5) == "Erika"
    assert _field(person, 7) == "1"
    assert _field(person, 18) == ""
    assert _field(person, 19) == "SW1A 1AA London"
    assert _field(person, 20) == "GB"
    assert _field(person, 245) == "1"

    # Text steht in Anführungszeichen, Zahlenfelder nicht.
    raw = content.split("\r\n")[2].split(";")
    assert raw[0] == both.debtor_number
    assert raw[1] == '"Doppel GmbH"'
    assert raw[6] == '"2"'
    assert raw[244] == "0"
    assert raw[109] == "14"


def test_partner_export_clips_to_format_lengths(session: Session) -> None:
    company, _ = _seed(session)
    create_partner(
        session=session,
        company=company,
        changed_by="t",
        name="Sehr Lange Firmenbezeichnung " * 3,
        is_customer=True,
        street="Lange Straße " * 5,
    )
    session.commit()
    content = build_datev_partner_export(
        session=session, company_id=company.id, generated_at=GENERATED_AT
    )
    row = [row for row in _partner_rows(content) if row][0]
    assert len(_field(row, 2)) <= 50
    assert len(_field(row, 8)) <= 15
    assert len(_field(row, 16)) <= 36


# ---------------------------------------------------------------------------
# Altdatenübernahme
# ---------------------------------------------------------------------------


def _legacy_item(session, company, account, reference, counterparty, *, item_type, iban=None):
    return create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=account.id,
            item_type=item_type,
            reference=reference,
            counterparty=counterparty,
            counterparty_iban=iban,
            entry_date=date(2026, 3, 1),
            amount=Decimal("100.00"),
            changed_by="t",
        ),
    )


def test_takeover_groups_by_normalized_name_and_creates_partner(session: Session) -> None:
    company, accounts = _seed(session)
    _legacy_item(
        session, company, accounts["1600"], "ER-1", "Lieferant  KG", item_type="payable", iban=IBAN
    )
    _legacy_item(
        session, company, accounts["1600"], "ER-2", "lieferant kg", item_type="payable", iban=IBAN
    )
    # Auf einem Konto ohne Kennzeichen nicht übernehmbar.
    plain = _legacy_item(
        session, company, accounts["1200"], "ER-3", "Lieferant KG", item_type="payable"
    )

    proposals = partner_takeover_proposals(session=session, company_id=company.id)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["item_count"] == 3
    assert proposal["usable_item_count"] == 2
    assert proposal["is_supplier"] is True
    assert proposal["is_customer"] is False
    assert proposal["ibans"] == [IBAN]
    assert proposal["partner_id"] is None

    result = apply_partner_takeover(
        session=session, company_id=company.id, key=proposal["key"], changed_by="t"
    )
    assert result["created"] is True
    assert len(result["linked_open_item_ids"]) == 2
    assert [entry["open_item_id"] for entry in result["skipped"]] == [plain.id]

    partner = session.get(BusinessPartner, result["partner_id"])
    assert partner.creditor_number is not None
    assert partner.debtor_number is None
    assert partner.iban == IBAN
    # Freitext bleibt unverändert.
    linked = session.execute(
        select(OpenItem).where(OpenItem.partner_id == partner.id)
    ).scalars().all()
    assert {item.counterparty for item in linked} == {"Lieferant  KG", "lieferant kg"}
    assert session.execute(
        select(AuditLog).where(AuditLog.action == "partner_assigned")
    ).scalars().first() is not None

    # Danach bleibt nur der nicht übernehmbare Posten als Vorschlag.
    remaining = partner_takeover_proposals(session=session, company_id=company.id)
    assert [row["usable_item_count"] for row in remaining] == [0]
    with pytest.raises(PartnerTakeoverError, match="Sammelkonto"):
        apply_partner_takeover(
            session=session, company_id=company.id, key=remaining[0]["key"], changed_by="t"
        )


def test_takeover_with_several_ibans_leaves_bank_details_to_a_human(session: Session) -> None:
    company, accounts = _seed(session)
    _legacy_item(
        session, company, accounts["1600"], "ER-1", "Zwei Konten", item_type="payable", iban=IBAN
    )
    _legacy_item(
        session,
        company,
        accounts["1600"],
        "ER-2",
        "Zwei Konten",
        item_type="payable",
        iban=OTHER_IBAN,
    )
    result = apply_partner_takeover(
        session=session, company_id=company.id, key="zwei konten", changed_by="t"
    )
    assert session.get(BusinessPartner, result["partner_id"]).iban is None


def test_takeover_suggests_existing_partner_and_checks_role(session: Session) -> None:
    company, accounts = _seed(session)
    customer = create_partner(
        session=session, company=company, changed_by="t", name="Bekannt GmbH", is_customer=True
    )
    supplier = create_partner(
        session=session, company=company, changed_by="t", name="Per IBAN", is_supplier=True
    )
    set_partner_bank_details(session=session, partner=supplier, changed_by="t", iban=IBAN, bic="")
    session.commit()
    _legacy_item(
        session, company, accounts["1400"], "RE-1", "bekannt  gmbh", item_type="receivable"
    )
    _legacy_item(
        session, company, accounts["1600"], "ER-1", "Anderer Name", item_type="payable", iban=IBAN
    )

    proposals = {
        row["key"]: row
        for row in partner_takeover_proposals(session=session, company_id=company.id)
    }
    assert proposals["bekannt gmbh"]["partner_id"] == customer.id
    assert proposals["anderer name"]["partner_id"] == supplier.id

    # Ein Lieferant passt nicht zur Forderung.
    with pytest.raises(PartnerTakeoverError, match="passende Rolle"):
        apply_partner_takeover(
            session=session,
            company_id=company.id,
            key="bekannt gmbh",
            partner_id=supplier.id,
            changed_by="t",
        )
    result = apply_partner_takeover(
        session=session,
        company_id=company.id,
        key="bekannt gmbh",
        partner_id=customer.id,
        changed_by="t",
    )
    assert result["created"] is False
    assert len(result["linked_open_item_ids"]) == 1


# ---------------------------------------------------------------------------
# REST und Oberfläche
# ---------------------------------------------------------------------------


def _client(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'datev.db'}"}
    )
    with app.extensions["db_session_factory"]() as db:
        company, accounts = _seed(db)
        db.add(
            User(
                username="admin",
                password_hash=hash_password("geheim123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db.commit()
        ids = {code: account.id for code, account in accounts.items()}
        company_id = company.id
    client = app.test_client()
    assert (
        client.post("/auth/login", data={"username": "admin", "password": "geheim123"}).status_code
        == 302
    )
    return app, client, company_id, ids


def test_api_and_ui_cover_export_and_takeover(tmp_path: Path) -> None:
    _, client, company_id, ids = _client(tmp_path)
    customer = client.post(
        "/api/v1/partners",
        json={"company_id": company_id, "name": "Kunde Ä", "is_customer": True},
    ).get_json()
    booked = client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": company_id,
            "entry_date": "2026-03-01",
            "description": "Rechnung",
            "lines": [
                {
                    "account_id": ids["1400"],
                    "debit_amount": "100.00",
                    "credit_amount": "0.00",
                    "partner_id": customer["id"],
                },
                {"account_id": ids["8400"], "debit_amount": "0.00", "credit_amount": "100.00"},
            ],
        },
    )
    assert booked.status_code == 201, booked.get_json()

    export = client.get("/api/v1/exports/datev.csv", query_string={"company_id": company_id})
    assert export.status_code == 200
    assert customer["debtor_number"] in export.get_data().decode("cp1252")
    without = client.get(
        "/api/v1/exports/datev.csv",
        query_string={"company_id": company_id, "personal_accounts": "0"},
    )
    assert customer["debtor_number"] not in without.get_data().decode("cp1252")

    partners = client.get(
        "/api/v1/exports/datev-partners.csv", query_string={"company_id": company_id}
    )
    assert partners.status_code == 200
    assert partners.headers["Content-Type"].startswith("text/csv")
    assert "EXTF_Debitoren_Kreditoren" in partners.headers["Content-Disposition"]
    assert "Kunde Ä" in partners.get_data().decode("cp1252")
    assert (
        client.get(
            "/api/v1/exports/datev-partners.csv", query_string={"company_id": 9999}
        ).status_code
        == 404
    )

    item = client.post(
        "/api/v1/open-items",
        json={
            "company_id": company_id,
            "account_id": ids["1400"],
            "item_type": "receivable",
            "reference": "RE-ALT",
            "amount": "50.00",
            "counterparty": "Altkunde",
        },
    ).get_json()
    proposals = client.get(
        "/api/v1/partners/takeover-proposals", query_string={"company_id": company_id}
    ).get_json()["proposals"]
    assert proposals[0]["key"] == "altkunde"
    assert proposals[0]["open_total"] == "50.00"

    page = client.get("/partner/uebernahme", query_string={"company_id": company_id}).get_data(
        as_text=True
    )
    assert "Altkunde" in page
    berichte = client.get("/berichte", query_string={"company_id": company_id})
    assert "auf Sammelkonten" in berichte.get_data(as_text=True)

    result = client.post(
        "/api/v1/partners/takeover",
        json={"company_id": company_id, "key": "altkunde"},
    )
    assert result.status_code == 200, result.get_json()
    assert result.get_json()["linked_open_item_ids"] == [item["id"]]
    assert (
        client.post(
            "/api/v1/partners/takeover", json={"company_id": company_id, "key": "altkunde"}
        ).status_code
        == 422
    )


def test_takeover_is_write_protected_and_tenant_scoped(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "API_REQUIRE_AUTH": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'rights.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db:
        company, _ = _seed(db)
        tenant = Tenant(name="Fremd")
        foreign = Company(tenant=tenant, name="Fremd GmbH", currency_code="EUR")
        db.add_all([tenant, foreign])
        db.flush()
        token = "obk_pruefer-datev"
        writer_token = "obk_buchhalter-datev"
        db.add(
            User(
                username="pruefer",
                password_hash=hash_password("geheim123"),
                role="Pruefer",
                tenant_id=company.tenant_id,
                api_token_hash=hash_api_token(token),
                api_token_last4=token[-4:],
            )
        )
        db.add(
            User(
                username="buchhalter",
                password_hash=hash_password("geheim123"),
                role="Buchhalter",
                tenant_id=company.tenant_id,
                api_token_hash=hash_api_token(writer_token),
                api_token_last4=writer_token[-4:],
            )
        )
        db.commit()
        company_id, foreign_id = company.id, foreign.id

    client = app.test_client()
    reader = {"Authorization": f"Bearer {token}"}
    writer = {"Authorization": f"Bearer {writer_token}"}
    assert (
        client.post(
            "/api/v1/partners/takeover",
            headers=reader,
            json={"company_id": company_id, "key": "x"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/partners/takeover-proposals",
            headers=reader,
            query_string={"company_id": company_id},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/partners/takeover",
            headers=writer,
            json={"company_id": foreign_id, "key": "x"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/api/v1/exports/datev-partners.csv",
            headers=writer,
            query_string={"company_id": foreign_id},
        ).status_code
        == 404
    )
