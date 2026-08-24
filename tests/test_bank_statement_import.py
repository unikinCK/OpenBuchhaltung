from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.bank_import import BankImportError, import_bank_statement
from app.services.bank_statement import (
    BankStatementParseError,
    BankStatementRow,
    detect_statement_format,
    parse_camt053,
    parse_mt940,
)
from domain.models import Account, BankTransaction, Base, Company, Tenant, User

CAMT_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <Stmt>
      <Id>STMT-2026-08</Id>
      <Ntry>
        <Amt Ccy="EUR">1500.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-08-05</Dt></BookgDt>
        <ValDt><Dt>2026-08-05</Dt></ValDt>
        <NtryDtls>
          <TxDtls>
            <RltdPties>
              <Dbtr><Nm>Mustermann Consulting GmbH</Nm></Dbtr>
            </RltdPties>
            <RmtInf>
              <Ustrd>RE 2026-015</Ustrd>
              <Ustrd>Projekt Alpha</Ustrd>
            </RmtInf>
          </TxDtls>
        </NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">123.45</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-08-02</Dt></BookgDt>
        <NtryDtls>
          <TxDtls>
            <RltdPties>
              <Cdtr><Pty><Nm>ACME Software GmbH</Nm></Pty></Cdtr>
            </RltdPties>
            <RmtInf><Ustrd>Rechnung 4711 Software-Abo</Ustrd></RmtInf>
          </TxDtls>
        </NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">9.90</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-08-03</Dt></BookgDt>
        <AddtlNtryInf>Entgeltabrechnung Kontofuehrung</AddtlNtryInf>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""

MT940_SAMPLE = (
    ":20:STARTUMSE\r\n"
    ":25:10010010/1234567890\r\n"
    ":28C:00001/001\r\n"
    ":60F:C260801EUR4321,00\r\n"
    ":61:2608020802DR123,45NTRFNONREF\r\n"
    ":86:105?00FOLGELASTSCHRIFT?109251?20EREF+STRIPE-REF-1?21MREF+M-77\r\n"
    "?22CRED+DE98ZZZ09999999999?23SVWZ+RECHNUNG 4711 SOFTWARE\r\n"
    "?24ABO AUGUST?32ACME SOFTWARE GMBH?34997\r\n"
    ":61:2608050805CR1500,00NTRFNONREF\r\n"
    ":86:166?00GUTSCHRIFT?109251?20EREF+KD-1001?21SVWZ+RE 2026-015 DANKE\r\n"
    "?32MUSTERMANN CONSULTING?33GMBH\r\n"
    ":62F:C260805EUR5697,55\r\n"
    "-"
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account]:
    tenant = Tenant(name="Statement Tenant")
    company = Company(tenant=tenant, name="Statement GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    session.add(bank)
    session.commit()
    return company, bank


def test_detect_statement_format_by_suffix_and_content():
    assert detect_statement_format("umsatz.csv", b"a;b") == "csv"
    assert detect_statement_format("auszug.xml", b"<Document/>") == "camt"
    assert detect_statement_format("auszug.sta", b":20:x") == "mt940"
    assert detect_statement_format("auszug.MT940", b":20:x") == "mt940"
    assert detect_statement_format("upload", b"  <?xml version='1.0'?><Document/>") == "camt"
    assert detect_statement_format("upload", b":20:REF\r\n:61:2608020802DR1,00N") == "mt940"
    assert detect_statement_format("upload", b"datum;betrag") == "csv"


def test_parse_camt053_extracts_rows():
    rows = parse_camt053(CAMT_SAMPLE.encode("utf-8"))
    assert [type(row) for row in rows] == [BankStatementRow] * 3

    incoming, outgoing, fee = rows
    assert incoming.amount == Decimal("1500.00")
    assert incoming.booking_date == date(2026, 8, 5)
    assert incoming.purpose == "RE 2026-015 Projekt Alpha"
    assert incoming.counterparty == "Mustermann Consulting GmbH"
    assert incoming.currency_code == "EUR"

    assert outgoing.amount == Decimal("-123.45")
    assert outgoing.counterparty == "ACME Software GmbH"

    assert fee.amount == Decimal("-9.90")
    assert fee.purpose == "Entgeltabrechnung Kontofuehrung"
    assert fee.counterparty is None


def test_parse_camt053_rejects_invalid_xml_and_empty_document():
    with pytest.raises(BankStatementParseError):
        parse_camt053(b"kein xml")
    with pytest.raises(BankStatementParseError):
        parse_camt053(b"<Document></Document>")


def test_parse_mt940_extracts_rows():
    rows = parse_mt940(MT940_SAMPLE.encode("utf-8"))
    assert [type(row) for row in rows] == [BankStatementRow] * 2

    outgoing, incoming = rows
    assert outgoing.amount == Decimal("-123.45")
    assert outgoing.booking_date == date(2026, 8, 2)
    assert "RECHNUNG 4711 SOFTWARE" in outgoing.purpose
    assert outgoing.counterparty == "ACME SOFTWARE GMBH"

    assert incoming.amount == Decimal("1500.00")
    assert incoming.booking_date == date(2026, 8, 5)
    assert incoming.counterparty == "MUSTERMANN CONSULTINGGMBH"


def test_parse_mt940_rejects_garbage():
    with pytest.raises(BankStatementParseError):
        parse_mt940(b"das ist kein kontoauszug")


def test_import_bank_statement_camt_and_dedup(session: Session):
    company, bank = _seed_company(session)

    report = import_bank_statement(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        file_name="auszug.xml",
        content=CAMT_SAMPLE.encode("utf-8"),
        changed_by="tester",
    )
    assert report.imported_rows == 3
    assert report.error_rows == 0

    # Re-Import derselben Datei ist idempotent.
    report = import_bank_statement(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        file_name="auszug.xml",
        content=CAMT_SAMPLE.encode("utf-8"),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.duplicate_rows == 3

    transactions = session.execute(select(BankTransaction)).scalars().all()
    assert len(transactions) == 3
    assert {tx.status for tx in transactions} == {"open"}


def test_import_bank_statement_mt940_latin1(session: Session):
    company, bank = _seed_company(session)
    content = MT940_SAMPLE.replace("ABO AUGUST", "GEBÜHR AUGUST").encode("latin-1")

    report = import_bank_statement(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        file_name="auszug.sta",
        content=content,
        changed_by="tester",
    )
    assert report.imported_rows == 2
    purposes = session.execute(select(BankTransaction.purpose)).scalars().all()
    assert any("GEBÜHR AUGUST" in purpose for purpose in purposes)


def test_import_bank_statement_invalid_camt_raises(session: Session):
    company, bank = _seed_company(session)
    with pytest.raises(BankImportError):
        import_bank_statement(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            file_name="auszug.xml",
            content=b"<Document></Document>",
            changed_by="tester",
        )


@pytest.fixture()
def api_client(tmp_path):
    db_path = tmp_path / "statement_api.sqlite"
    app = create_app(
        {
            "DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
            "TESTING": True,
            "SECRET_KEY": "test",
            "API_REQUIRE_AUTH": False,
            "CSRF_PROTECT": False,
        }
    )
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        tenant = Tenant(name="API Statement Tenant")
        company = Company(tenant=tenant, name="API Statement GmbH", currency_code="EUR")
        session.add_all([tenant, company])
        session.flush()
        bank = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        user = User(
            tenant_id=tenant.id,
            username="statement-admin",
            password_hash=hash_password("secret"),
            role="admin",
        )
        session.add_all([bank, user])
        session.commit()
        ids = (company.id, bank.id)
    with app.test_client() as client:
        yield client, ids


def test_api_import_camt_statement(api_client):
    client, (company_id, bank_account_id) = api_client
    response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": company_id,
            "bank_account_id": bank_account_id,
            "file_name": "auszug.xml",
            "mime_type": "application/xml",
            "content_base64": base64.b64encode(CAMT_SAMPLE.encode("utf-8")).decode("ascii"),
        },
    )
    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["report"]["imported_rows"] == 3
    assert len(payload["transactions"]) == 3


def test_api_import_mt940_statement(api_client):
    client, (company_id, bank_account_id) = api_client
    response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": company_id,
            "bank_account_id": bank_account_id,
            "file_name": "auszug.sta",
            "mime_type": "application/octet-stream",
            "content_base64": base64.b64encode(MT940_SAMPLE.encode("utf-8")).decode("ascii"),
        },
    )
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["report"]["imported_rows"] == 2


def test_api_import_rejects_unknown_extension(api_client):
    client, (company_id, bank_account_id) = api_client
    response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": company_id,
            "bank_account_id": bank_account_id,
            "file_name": "auszug.pdf",
            "mime_type": "application/octet-stream",
            "content_base64": base64.b64encode(b"%PDF-1.4").decode("ascii"),
        },
    )
    assert response.status_code == 422
