"""SEPA-Zahllauf: IBAN-Validierung und pain.001-Erzeugung."""

from __future__ import annotations

import base64
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.open_items import OpenItemInput, create_open_item
from app.services.sepa_export import (
    SepaExportError,
    create_payment_run,
    normalize_iban,
    payable_items_for_run,
    set_company_bank_details,
)
from domain.models import Account, AuditLog, Base, Company, Tenant, User

NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"}
VALID_IBAN = "DE89370400440532013000"
VALID_IBAN_2 = "DE02100100109307118603"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session):
    tenant = Tenant(name="SEPA Tenant")
    company = Company(tenant=tenant, name="SEPA GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    payable_account = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1600",
        name="Verbindlichkeiten",
        account_type="liability",
    )
    session.add(payable_account)
    session.commit()
    return company, payable_account


def _payable(session, company, account, *, reference, iban=None, bic=None):
    return create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=account.id,
            item_type="payable",
            reference=reference,
            counterparty="ACME Software GmbH",
            entry_date=date.today() - timedelta(days=10),
            due_date=date.today() + timedelta(days=4),
            amount=Decimal("119.00"),
            changed_by="tester",
            counterparty_iban=iban,
            counterparty_bic=bic,
        ),
    )


def test_normalize_iban_accepts_valid_and_rejects_invalid() -> None:
    assert normalize_iban("de89 3704 0044 0532 0130 00") == VALID_IBAN
    assert normalize_iban(None) is None
    assert normalize_iban("") is None
    with pytest.raises(SepaExportError, match="Prüfsumme"):
        normalize_iban("DE89370400440532013001")
    with pytest.raises(SepaExportError, match="Ungültige IBAN"):
        normalize_iban("123")


def test_create_payment_run_builds_valid_pain001(session: Session) -> None:
    company, account = _seed(session)
    set_company_bank_details(
        session=session,
        company_id=company.id,
        iban=VALID_IBAN,
        bic="MARKDEF1100",
        changed_by="tester",
    )
    item_a = _payable(session, company, account, reference="ER-1", iban=VALID_IBAN_2)
    item_b = _payable(
        session, company, account, reference="ER-2", iban=VALID_IBAN_2, bic="INGDDEFFXXX"
    )
    _payable(session, company, account, reference="ER-3")  # ohne IBAN

    proposals = payable_items_for_run(session=session, company_id=company.id)
    assert [item.reference for item in proposals] == ["ER-1", "ER-2"]

    result = create_payment_run(
        session=session,
        company_id=company.id,
        open_item_ids=[item_a.id, item_b.id],
        changed_by="tester",
    )
    assert result.transaction_count == 2
    assert result.control_sum == Decimal("238.00")

    root = ET.fromstring(result.xml_bytes)
    assert root.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:NbOfTxs", namespaces=NS) == "2"
    assert (
        root.findtext("p:CstmrCdtTrfInitn/p:GrpHdr/p:CtrlSum", namespaces=NS) == "238.00"
    )
    payment_info = root.find("p:CstmrCdtTrfInitn/p:PmtInf", namespaces=NS)
    assert payment_info.findtext("p:DbtrAcct/p:Id/p:IBAN", namespaces=NS) == VALID_IBAN
    transfers = payment_info.findall("p:CdtTrfTxInf", namespaces=NS)
    assert [t.findtext("p:PmtId/p:EndToEndId", namespaces=NS) for t in transfers] == [
        "ER-1",
        "ER-2",
    ]
    assert transfers[0].findtext("p:CdtrAcct/p:Id/p:IBAN", namespaces=NS) == VALID_IBAN_2
    assert transfers[1].findtext("p:CdtrAgt/p:FinInstnId/p:BIC", namespaces=NS) == (
        "INGDDEFFXXX"
    )

    audit = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "payment_run")
    ).scalar_one()
    assert audit.payload["transaction_count"] == 2


def test_create_payment_run_validates_items(session: Session) -> None:
    company, account = _seed(session)

    with pytest.raises(SepaExportError, match="Auftraggeber-IBAN"):
        create_payment_run(
            session=session, company_id=company.id, open_item_ids=[1], changed_by="t"
        )

    set_company_bank_details(
        session=session, company_id=company.id, iban=VALID_IBAN, bic=None, changed_by="t"
    )
    no_iban = _payable(session, company, account, reference="ER-9")
    with pytest.raises(SepaExportError, match="keine Empfänger-IBAN"):
        create_payment_run(
            session=session,
            company_id=company.id,
            open_item_ids=[no_iban.id],
            changed_by="t",
        )
    with pytest.raises(SepaExportError, match="Vergangenheit"):
        item = _payable(session, company, account, reference="ER-10", iban=VALID_IBAN_2)
        create_payment_run(
            session=session,
            company_id=company.id,
            open_item_ids=[item.id],
            execution_date=date.today() - timedelta(days=1),
            changed_by="t",
        )


def test_payment_run_web_and_api_flow(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'sepa.db'}"}
    )
    with app.extensions["db_session_factory"]() as db_session:
        company, account = _seed(db_session)
        item = _payable(db_session, company, account, reference="ER-42", iban=VALID_IBAN_2)
        db_session.add(
            User(
                username="zahler",
                password_hash=hash_password("pw"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
        company_id, item_id = company.id, item.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "zahler", "password": "pw"})

    saved = client.post(
        f"/api/v1/companies/{company_id}/bank-details",
        json={"iban": VALID_IBAN, "bic": "MARKDEF1100"},
    )
    assert saved.status_code == 200
    assert saved.get_json()["iban"] == VALID_IBAN

    page = client.get(f"/zahllauf?company_id={company_id}")
    assert page.status_code == 200
    assert b"ER-42" in page.data

    proposals = client.get(f"/api/v1/payment-runs/proposals?company_id={company_id}")
    assert proposals.get_json()["items"][0]["reference"] == "ER-42"

    run = client.post(
        "/api/v1/payment-runs",
        json={"company_id": company_id, "open_item_ids": [item_id]},
    )
    assert run.status_code == 201
    body = run.get_json()
    assert body["transaction_count"] == 1
    xml = base64.b64decode(body["xml_base64"])
    assert b"pain.001.001.03" in xml

    download = client.post(
        "/zahllauf",
        data={
            "company_id": str(company_id),
            "open_item_ids": str(item_id),
            "execution_date": date.today().isoformat(),
        },
    )
    assert download.status_code == 200
    assert download.mimetype == "application/xml"
    assert b"CstmrCdtTrfInitn" in download.data
