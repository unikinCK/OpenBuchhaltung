from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fints.client import NeedTANResponse
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services import fints_sync
from app.services.fints_sync import (
    FinTSSyncError,
    create_fints_connection,
    list_fints_connections,
    set_fints_connection_active,
    start_fints_sync,
    submit_fints_tan,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    FinTSPendingDialog,
    Tenant,
    User,
)

PRODUCT_ID = "TEST-PRODUCT-ID"


class FakeTanRequest(NeedTANResponse):
    """Echtes NeedTANResponse-Subtyping, aber ohne FinTS-Segmente."""

    def __init__(self, challenge: str = "Bitte TAN eingeben", decoupled: bool = False):
        self.challenge = challenge
        self.decoupled = decoupled

    def get_data(self) -> bytes:
        return b"fake-tan-request"


class FakeAmount:
    def __init__(self, amount: str, currency: str = "EUR"):
        self.amount = Decimal(amount)
        self.currency = currency


def _fake_transaction(amount: str, day: int, purpose: str, applicant: str):
    return SimpleNamespace(
        data={
            "amount": FakeAmount(amount),
            "date": date(2026, 8, day),
            "entry_date": date(2026, 8, day),
            "purpose": purpose,
            "applicant_name": applicant,
            "currency": "EUR",
        }
    )


FAKE_TRANSACTIONS = [
    _fake_transaction("1500.00", 5, "RE 2026-015", "Mustermann Consulting"),
    _fake_transaction("-123.45", 2, "Rechnung 4711", "ACME Software GmbH"),
]


class FakeFinTSClient:
    """Simuliert FinTS3PinTanClient inkl. Pause/Resume und TAN-Pfaden.

    ``script`` steuert das Verhalten: "ok" (Umsätze sofort), "tan_init"
    (TAN bei Dialog-Init), "tan_transactions" (TAN beim Umsatzabruf),
    "decoupled_pending" (pushTAN noch nicht bestätigt).
    """

    def __init__(self, script: str = "ok", accounts=None):
        self.script = script
        self.accounts = accounts or [SimpleNamespace(iban="DE02100100109307118603")]
        self.init_tan_response = None
        self.selected_tan_medium = ""
        self.sent_tan = None

    # Kontextprotokoll ---------------------------------------------------
    def __enter__(self):
        if self.script == "tan_init":
            self.init_tan_response = FakeTanRequest("Dialog-Init: TAN nötig")
        return self

    def __exit__(self, *exc):
        return False

    @contextmanager
    def resume_dialog(self, dialog_data):
        assert dialog_data == b"dialog-data"
        yield self

    # TAN-Verwaltung -----------------------------------------------------
    def get_current_tan_mechanism(self):
        return "942"

    # Abruf --------------------------------------------------------------
    def get_sepa_accounts(self):
        return self.accounts

    def get_transactions(self, account, start_date, end_date):
        if self.script == "tan_transactions" and self.sent_tan is None:
            return FakeTanRequest("Umsatzabruf: TAN nötig")
        return FAKE_TRANSACTIONS

    def send_tan(self, challenge, tan):
        self.sent_tan = tan
        if self.script == "decoupled_pending":
            return FakeTanRequest("Noch nicht freigegeben", decoupled=True)
        if self.script == "tan_init":
            # Nach der Init-TAN läuft der Abruf normal weiter.
            self.script = "ok"
            return SimpleNamespace()
        return FAKE_TRANSACTIONS

    def pause_dialog(self):
        return b"dialog-data"

    def deconstruct(self, including_private: bool = False):
        assert including_private is True
        return b"client-data"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account]:
    tenant = Tenant(name="FinTS Tenant")
    company = Company(tenant=tenant, name="FinTS GmbH", currency_code="EUR")
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


def _create_connection(session: Session, company: Company, bank: Account):
    return create_fints_connection(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        name="Geschäftskonto",
        blz="10010010",
        login="kunde1",
        fints_url="https://fints.example.de/fints30",
        changed_by="tester",
    )


def _patch_client(monkeypatch, client: FakeFinTSClient):
    created = []

    def factory(connection, pin, product_id, from_data=None):
        created.append({"pin": pin, "product_id": product_id, "from_data": from_data})
        return client

    monkeypatch.setattr(fints_sync, "_build_client", factory)
    monkeypatch.setattr(
        fints_sync, "_tan_request_from_data", lambda data: FakeTanRequest("gespeichert")
    )
    return created


# ---------------------------------------------------------------------------
# Verwaltung


def test_create_connection_validates_input(session: Session):
    company, bank = _seed_company(session)

    with pytest.raises(FinTSSyncError, match="BLZ"):
        create_fints_connection(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            name="X",
            blz="123",
            login="kunde1",
            fints_url="https://fints.example.de",
            changed_by="tester",
        )
    with pytest.raises(FinTSSyncError, match="https"):
        create_fints_connection(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            name="X",
            blz="10010010",
            login="kunde1",
            fints_url="http://unsicher.example.de",
            changed_by="tester",
        )


def test_create_list_and_deactivate_connection(session: Session):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    assert connection.is_active is True

    with pytest.raises(FinTSSyncError, match="existiert bereits"):
        _create_connection(session, company, bank)

    assert len(list_fints_connections(session=session, company_id=company.id)) == 1

    set_fints_connection_active(
        session=session, connection_id=connection.id, is_active=False, changed_by="tester"
    )
    assert list_fints_connections(session=session, company_id=company.id) == []
    assert (
        len(
            list_fints_connections(
                session=session, company_id=company.id, include_inactive=True
            )
        )
        == 1
    )

    actions = session.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "fints_connection")
    ).scalars().all()
    assert actions == ["created", "deactivated"]


# ---------------------------------------------------------------------------
# Abruf ohne TAN


def test_sync_imports_transactions(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    created = _patch_client(monkeypatch, FakeFinTSClient("ok"))

    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert result.challenge is None
    assert result.report.imported_rows == 2
    assert created[0]["product_id"] == PRODUCT_ID

    transactions = session.execute(select(BankTransaction)).scalars().all()
    assert {tx.amount for tx in transactions} == {Decimal("1500.00"), Decimal("-123.45")}
    assert {tx.status for tx in transactions} == {"open"}

    # Zweiter Abruf: alles Duplikate.
    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert result.report.duplicate_rows == 2


def test_sync_requires_pin_and_product_id(session: Session):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)

    with pytest.raises(FinTSSyncError, match="PIN"):
        start_fints_sync(
            session=session,
            connection_id=connection.id,
            pin="",
            product_id=PRODUCT_ID,
            changed_by="tester",
        )
    with pytest.raises(FinTSSyncError, match="FINTS_PRODUCT_ID"):
        start_fints_sync(
            session=session,
            connection_id=connection.id,
            pin="1234",
            product_id=None,
            changed_by="tester",
        )


def test_sync_rejects_ambiguous_accounts_without_iban(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    accounts = [SimpleNamespace(iban="DE1"), SimpleNamespace(iban="DE2")]
    _patch_client(monkeypatch, FakeFinTSClient("ok", accounts=accounts))

    with pytest.raises(FinTSSyncError, match="mehrere Konten"):
        start_fints_sync(
            session=session,
            connection_id=connection.id,
            pin="1234",
            product_id=PRODUCT_ID,
            changed_by="tester",
        )


# ---------------------------------------------------------------------------
# TAN-Pfade


def test_sync_with_tan_at_transactions_step(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    client = FakeFinTSClient("tan_transactions")
    _patch_client(monkeypatch, client)

    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert result.report is None
    assert result.challenge is not None
    assert "TAN" in result.challenge.challenge
    assert result.challenge.decoupled is False

    pending = session.get(FinTSPendingDialog, result.challenge.dialog_id)
    assert pending is not None
    assert pending.step == "transactions"

    tan_result = submit_fints_tan(
        session=session,
        dialog_id=result.challenge.dialog_id,
        pin="1234",
        tan="987654",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert tan_result.report.imported_rows == 2
    assert client.sent_tan == "987654"
    assert session.get(FinTSPendingDialog, result.challenge.dialog_id) is None


def test_sync_with_tan_at_dialog_init(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    client = FakeFinTSClient("tan_init")
    _patch_client(monkeypatch, client)

    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert result.challenge is not None
    pending = session.get(FinTSPendingDialog, result.challenge.dialog_id)
    assert pending.step == "init"

    tan_result = submit_fints_tan(
        session=session,
        dialog_id=result.challenge.dialog_id,
        pin="1234",
        tan="111111",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert tan_result.report.imported_rows == 2


def test_decoupled_tan_returns_new_challenge(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    client = FakeFinTSClient("tan_transactions")
    _patch_client(monkeypatch, client)

    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    dialog_id = result.challenge.dialog_id

    # Bank meldet: Freigabe in der App steht noch aus.
    client.script = "decoupled_pending"
    tan_result = submit_fints_tan(
        session=session,
        dialog_id=dialog_id,
        pin="1234",
        tan=None,
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert tan_result.report is None
    assert tan_result.challenge.decoupled is True
    # Derselbe Dialog bleibt bestehen (aktualisiert, nicht dupliziert).
    assert tan_result.challenge.dialog_id == dialog_id
    assert session.get(FinTSPendingDialog, dialog_id) is not None

    # Nach der Freigabe liefert send_tan die Umsätze.
    client.script = "tan_transactions"
    final = submit_fints_tan(
        session=session,
        dialog_id=dialog_id,
        pin="1234",
        tan=None,
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    assert final.report.imported_rows == 2


def test_expired_dialog_is_rejected(session: Session, monkeypatch):
    company, bank = _seed_company(session)
    connection = _create_connection(session, company, bank)
    client = FakeFinTSClient("tan_transactions")
    _patch_client(monkeypatch, client)

    result = start_fints_sync(
        session=session,
        connection_id=connection.id,
        pin="1234",
        product_id=PRODUCT_ID,
        changed_by="tester",
    )
    pending = session.get(FinTSPendingDialog, result.challenge.dialog_id)
    pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    session.commit()

    with pytest.raises(FinTSSyncError, match="abgelaufen"):
        submit_fints_tan(
            session=session,
            dialog_id=result.challenge.dialog_id,
            pin="1234",
            tan="1",
            product_id=PRODUCT_ID,
            changed_by="tester",
        )
    assert session.get(FinTSPendingDialog, result.challenge.dialog_id) is None


# ---------------------------------------------------------------------------
# API-Schicht


@pytest.fixture()
def api_app(tmp_path):
    db_path = tmp_path / "fints_api.sqlite"
    app = create_app(
        {
            "DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
            "TESTING": True,
            "SECRET_KEY": "test",
            "API_REQUIRE_AUTH": False,
            "CSRF_PROTECT": False,
            "FINTS_PRODUCT_ID": PRODUCT_ID,
        }
    )
    session_factory = app.extensions["db_session_factory"]
    with session_factory() as session:
        tenant = Tenant(name="FinTS API Tenant")
        company = Company(tenant=tenant, name="FinTS API GmbH", currency_code="EUR")
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
            username="fints-admin",
            password_hash=hash_password("secret"),
            role="admin",
        )
        session.add_all([bank, user])
        session.commit()
        ids = (company.id, bank.id)
    return app, ids


def test_api_connection_lifecycle_and_sync(api_app, monkeypatch):
    app, (company_id, bank_account_id) = api_app
    client_fints = FakeFinTSClient("ok")
    _patch_client(monkeypatch, client_fints)

    with app.test_client() as client:
        created = client.post(
            "/api/v1/fints-connections",
            json={
                "company_id": company_id,
                "bank_account_id": bank_account_id,
                "name": "Geschäftskonto",
                "blz": "10010010",
                "login": "kunde1",
                "fints_url": "https://fints.example.de/fints30",
            },
        )
        assert created.status_code == 201, created.get_json()
        connection_id = created.get_json()["id"]

        listing = client.get(
            "/api/v1/fints-connections", query_string={"company_id": company_id}
        )
        assert listing.status_code == 200
        assert len(listing.get_json()["connections"]) == 1

        sync = client.post(
            f"/api/v1/fints-connections/{connection_id}/sync",
            json={"pin": "1234"},
        )
        assert sync.status_code == 200, sync.get_json()
        payload = sync.get_json()
        assert payload["status"] == "imported"
        assert payload["report"]["imported_rows"] == 2

        deactivated = client.post(
            f"/api/v1/fints-connections/{connection_id}/active",
            json={"is_active": False},
        )
        assert deactivated.status_code == 200
        assert deactivated.get_json()["is_active"] is False

        rejected = client.post(
            f"/api/v1/fints-connections/{connection_id}/sync", json={"pin": "1234"}
        )
        assert rejected.status_code == 422


def test_api_sync_tan_flow(api_app, monkeypatch):
    app, (company_id, bank_account_id) = api_app
    client_fints = FakeFinTSClient("tan_transactions")
    _patch_client(monkeypatch, client_fints)

    with app.test_client() as client:
        connection_id = client.post(
            "/api/v1/fints-connections",
            json={
                "company_id": company_id,
                "bank_account_id": bank_account_id,
                "name": "Geschäftskonto",
                "blz": "10010010",
                "login": "kunde1",
                "fints_url": "https://fints.example.de/fints30",
            },
        ).get_json()["id"]

        sync = client.post(
            f"/api/v1/fints-connections/{connection_id}/sync", json={"pin": "1234"}
        )
        assert sync.status_code == 202, sync.get_json()
        payload = sync.get_json()
        assert payload["status"] == "tan_required"

        tan = client.post(
            f"/api/v1/fints-dialogs/{payload['dialog_id']}/tan",
            json={"pin": "1234", "tan": "987654"},
        )
        assert tan.status_code == 200, tan.get_json()
        assert tan.get_json()["report"]["imported_rows"] == 2
