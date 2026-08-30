"""Mahnwesen: Vorschläge, Mahnstufen und Mahnschreiben."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.dunning import (
    DunningError,
    dunning_proposals,
    record_dunning,
)
from app.services.open_items import OpenItemInput, create_open_item
from domain.models import Account, AuditLog, Base, Company, Tenant, User


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session):
    tenant = Tenant(name="Mahn Tenant")
    company = Company(tenant=tenant, name="Mahn GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    receivable = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1400",
        name="Forderungen",
        account_type="asset",
    )
    session.add(receivable)
    session.commit()
    return company, receivable


def _item(session, company, account, *, reference, due_days_ago, item_type="receivable"):
    return create_open_item(
        session=session,
        payload=OpenItemInput(
            company_id=company.id,
            account_id=account.id,
            item_type=item_type,
            reference=reference,
            counterparty="Kunde AG",
            entry_date=date.today() - timedelta(days=due_days_ago + 14),
            due_date=date.today() - timedelta(days=due_days_ago),
            amount=Decimal("119.00"),
            changed_by="tester",
        ),
    )


def test_proposals_only_overdue_receivables(session: Session) -> None:
    company, account = _seed(session)
    overdue = _item(session, company, account, reference="RE-1", due_days_ago=10)
    _item(session, company, account, reference="RE-2", due_days_ago=-5)  # noch nicht fällig
    _item(session, company, account, reference="ER-1", due_days_ago=10, item_type="payable")

    proposals = dunning_proposals(session=session, company_id=company.id)
    assert [p.item.reference for p in proposals] == ["RE-1"]
    assert proposals[0].days_overdue == 10
    assert proposals[0].suggested_level == 1
    assert proposals[0].item.id == overdue.id


def test_record_dunning_levels_and_grace_period(session: Session) -> None:
    company, account = _seed(session)
    item = _item(session, company, account, reference="RE-1", due_days_ago=30)

    item = record_dunning(session=session, open_item_id=item.id, changed_by="tester")
    assert item.dunning_level == 1
    assert item.last_dunning_date == date.today()

    # Innerhalb der Schonfrist kein neuer Vorschlag.
    assert dunning_proposals(session=session, company_id=company.id) == []

    # Nach Ablauf der Schonfrist Vorschlag für Stufe 2.
    item.last_dunning_date = date.today() - timedelta(days=8)
    session.commit()
    proposals = dunning_proposals(session=session, company_id=company.id)
    assert proposals[0].suggested_level == 2

    item = record_dunning(session=session, open_item_id=item.id, changed_by="tester")
    item = record_dunning(session=session, open_item_id=item.id, changed_by="tester")
    assert item.dunning_level == 3

    with pytest.raises(DunningError, match="Mahnstufe"):
        record_dunning(session=session, open_item_id=item.id, changed_by="tester")

    # Höchststufe erreicht -> kein Vorschlag mehr.
    item.last_dunning_date = date.today() - timedelta(days=30)
    session.commit()
    assert dunning_proposals(session=session, company_id=company.id) == []

    actions = (
        session.execute(
            select(AuditLog.action).where(AuditLog.entity_type == "open_item")
        )
        .scalars()
        .all()
    )
    assert actions.count("dunned") == 3


def test_record_dunning_rejects_invalid_items(session: Session) -> None:
    company, account = _seed(session)
    not_due = _item(session, company, account, reference="RE-2", due_days_ago=-5)
    payable = _item(
        session, company, account, reference="ER-1", due_days_ago=10, item_type="payable"
    )

    with pytest.raises(DunningError, match="überfällig"):
        record_dunning(session=session, open_item_id=not_due.id, changed_by="t")
    with pytest.raises(DunningError, match="debitorische"):
        record_dunning(session=session, open_item_id=payable.id, changed_by="t")


def test_dunning_web_and_api_flow(tmp_path: Path):
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'dunning.db'}"}
    )
    with app.extensions["db_session_factory"]() as db_session:
        company, account = _seed(db_session)
        item = _item(db_session, company, account, reference="RE-77", due_days_ago=21)
        db_session.add(
            User(
                username="mahner",
                password_hash=hash_password("pw"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
        company_id, item_id = company.id, item.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "mahner", "password": "pw"})

    page = client.get(f"/mahnwesen?company_id={company_id}")
    assert page.status_code == 200
    assert b"RE-77" in page.data
    assert "Zahlungserinnerung".encode() in page.data

    letter = client.get(f"/mahnwesen/{item_id}/schreiben")
    assert letter.status_code == 200
    assert "Zahlungserinnerung".encode() in letter.data
    assert b"RE-77" in letter.data

    api_proposals = client.get(f"/api/v1/dunning-proposals?company_id={company_id}")
    assert api_proposals.status_code == 200
    assert api_proposals.get_json()["proposals"][0]["reference"] == "RE-77"

    recorded = client.post(f"/api/v1/open-items/{item_id}/dunning", json={})
    assert recorded.status_code == 200
    body = recorded.get_json()
    assert body["dunning_level"] == 1
    assert body["last_dunning_date"] == date.today().isoformat()

    # UI-Weg für Stufe 2 nach Ablauf der Schonfrist.
    with app.extensions["db_session_factory"]() as db_session:
        from domain.models import OpenItem

        db_item = db_session.get(OpenItem, item_id)
        db_item.last_dunning_date = date.today() - timedelta(days=10)
        db_session.commit()

    posted = client.post(f"/mahnwesen/{item_id}/mahnen", follow_redirects=True)
    assert posted.status_code == 200
    assert "Mahnstufe 2".encode() in posted.data
