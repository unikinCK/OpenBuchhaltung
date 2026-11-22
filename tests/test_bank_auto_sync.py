"""Täglicher automatischer Umsatzabruf eingerichteter Bankzugänge (FinTS, Open Banking, MCP)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import requests
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_fints import PRODUCT_ID, FakeFinTSClient, _patch_client
from test_mcp_bank_sync import (  # noqa: F401 – bank_app ist eine Fixture
    FINOM_TOOLS,
    WALLET,
    FakeFinom,
    _tx,
    bank_app,
)
from test_mcp_connections import _auth, _login

from app import create_app
from app.auth import ROLE_BUCHHALTER, hash_password
from app.services import bank_auto_sync, mcp_bank_sync, open_banking
from app.services.bank_auto_sync import (
    PAUSE_BANK_ERROR,
    PAUSE_TAN_REQUIRED,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TAN_REQUIRED,
    BankAutoSyncError,
    after_manual_fints_sync,
    disable_bank_auto_sync,
    due_now,
    enable_fints_auto_sync,
    enable_mcp_auto_sync,
    enable_open_banking_auto_sync,
    run_bank_auto_syncs,
    serialize_auto_sync,
    sync_time,
)
from app.services.demo import demo_block_reason
from app.services.fints_sync import create_fints_connection, set_fints_connection_active
from domain.models import (
    Account,
    AuditLog,
    BankAutoSync,
    BankTransaction,
    Base,
    Company,
    FinTSPendingDialog,
    McpServer,
    OpenBankingConnection,
    Tenant,
    User,
)

SECRET = "test-secret"
CONFIG = {"SECRET_KEY": SECRET, "FINTS_PRODUCT_ID": PRODUCT_ID, "BANK_AUTO_SYNC_TIME": "06:00"}
# 10:00 Uhr in Berlin am 20.11.2026.
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'auto.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=True)


@pytest.fixture()
def seeded(factory):
    with factory() as session:
        tenant = Tenant(name="Auto")
        company = Company(tenant=tenant, name="Auto GmbH", currency_code="EUR")
        session.add_all([tenant, company])
        session.flush()
        bank = Account(
            tenant_id=tenant.id, company_id=company.id, code="1200", name="Bank",
            account_type="asset",
        )
        user = User(
            tenant_id=tenant.id, username="anna", password_hash="x", role=ROLE_BUCHHALTER
        )
        session.add_all([bank, user])
        session.commit()
        connection = create_fints_connection(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            name="Sparkasse",
            blz="10010010",
            login="kunde1",
            fints_url="https://fints.example.de/fints30",
            changed_by="anna",
        )
        return {
            "company": company.id,
            "tenant": tenant.id,
            "bank": bank.id,
            "user": user.id,
            "fints": connection.id,
        }


def _enable_fints(factory, ids, pin="1234") -> int:
    with factory() as session:
        entry = enable_fints_auto_sync(
            session,
            connection_id=ids["fints"],
            pin=pin,
            secret_key=SECRET,
            user_id=ids["user"],
            changed_by="anna",
        )
        return entry.id


def _entry(factory, entry_id) -> BankAutoSync:
    with factory() as session:
        entry = session.get(BankAutoSync, entry_id)
        session.expunge(entry)
        return entry


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def test_sync_time_and_due_now():
    assert sync_time({}).strftime("%H:%M") == "06:00"
    assert sync_time({"BANK_AUTO_SYNC_TIME": "7:30"}).strftime("%H:%M") == "07:30"
    with pytest.raises(BankAutoSyncError, match="HH:MM"):
        sync_time({"BANK_AUTO_SYNC_TIME": "morgens"})
    # 05:30 UTC = 06:30 Berlin (Winterzeit)
    assert due_now(CONFIG, datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc))
    assert not due_now(CONFIG, datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# FinTS
# ---------------------------------------------------------------------------


def test_fints_pin_is_stored_encrypted_and_never_serialized(factory, seeded):
    entry_id = _enable_fints(factory, seeded, pin="geheim-4711")
    with factory() as session:
        entry = session.get(BankAutoSync, entry_id)
        assert entry.secret_encrypted and "geheim-4711" not in entry.secret_encrypted
        data = serialize_auto_sync(entry)
        assert data["pin_stored"] is True and data["name"] == "Sparkasse"
        assert "geheim-4711" not in repr(data) and entry.secret_encrypted not in repr(data)
        audit = session.execute(
            select(AuditLog.payload).where(AuditLog.entity_type == "bank_auto_sync")
        ).scalars().all()
        assert audit and "geheim-4711" not in repr(audit)
        with pytest.raises(BankAutoSyncError, match="PIN"):
            enable_fints_auto_sync(
                session, connection_id=seeded["fints"], pin=" ", secret_key=SECRET,
                user_id=None, changed_by="anna",
            )


def test_fints_run_imports_once_per_day_with_the_stored_pin(factory, seeded, monkeypatch):
    entry_id = _enable_fints(factory, seeded)
    created = _patch_client(monkeypatch, FakeFinTSClient("ok"))

    runs = run_bank_auto_syncs(factory, config=CONFIG, only_due=True, now=NOW)
    assert [(run.status, run.imported) for run in runs] == [(STATUS_OK, 2)]
    assert created[0]["pin"] == "1234" and created[0]["product_id"] == PRODUCT_ID
    entry = _entry(factory, entry_id)
    assert entry.last_status == STATUS_OK and entry.last_imported == 2
    assert entry.last_synced_to == date(2026, 11, 20)
    with factory() as session:
        assert len(session.execute(select(BankTransaction)).scalars().all()) == 2
        actors = set(session.execute(select(AuditLog.changed_by)).scalars())
    assert "anna (automatisch)" in actors

    # Am selben Tag nicht noch einmal, am nächsten wieder – mit Überlappung.
    assert run_bank_auto_syncs(factory, config=CONFIG, only_due=True, now=NOW) == []
    seen = {}
    original = FakeFinTSClient.get_transactions

    def spy(self, account, start_date, end_date):
        seen.update(start=start_date, end=end_date)
        return original(self, account, start_date, end_date)

    monkeypatch.setattr(FakeFinTSClient, "get_transactions", spy)
    runs = run_bank_auto_syncs(
        factory, config=CONFIG, only_due=True, now=NOW + timedelta(days=1)
    )
    assert runs[0].status == STATUS_OK and runs[0].imported == 0
    assert seen == {"start": date(2026, 11, 13), "end": date(2026, 11, 21)}


def test_fints_tan_request_pauses_until_a_manual_sync_succeeds(factory, seeded, monkeypatch):
    entry_id = _enable_fints(factory, seeded)
    _patch_client(monkeypatch, FakeFinTSClient("tan_transactions"))

    runs = run_bank_auto_syncs(factory, config=CONFIG, now=NOW)
    assert runs[0].status == STATUS_TAN_REQUIRED
    entry = _entry(factory, entry_id)
    assert entry.paused_reason == PAUSE_TAN_REQUIRED
    with factory() as session:
        # Der eingefrorene Dialog wird verworfen, nicht für niemanden aufgehoben.
        assert session.execute(select(FinTSPendingDialog)).scalars().all() == []
    # Angehalten: kein weiterer Versuch (kein täglicher pushTAN-Auftrag).
    assert run_bank_auto_syncs(factory, config=CONFIG, now=NOW + timedelta(days=1)) == []

    with factory() as session:
        resumed = after_manual_fints_sync(
            session, connection_id=seeded["fints"], pin="5678", secret_key=SECRET,
            changed_by="anna",
        )
    assert resumed is True
    entry = _entry(factory, entry_id)
    assert entry.paused_reason is None
    assert bank_auto_sync._decrypt_pin(entry.secret_encrypted, SECRET) == "5678"


def test_fints_bank_error_pauses_but_connection_problem_does_not(factory, seeded, monkeypatch):
    entry_id = _enable_fints(factory, seeded)

    class Unreachable(FakeFinTSClient):
        def get_sepa_accounts(self):
            # Wie python-fints: Der Verbindungsfehler steckt in einem FinTSDialogInitError.
            from fints.exceptions import FinTSDialogInitError

            try:
                raise requests.ConnectionError("Bank nicht erreichbar")
            except requests.ConnectionError as exc:
                raise FinTSDialogInitError("Authentication data wrong?") from exc

    _patch_client(monkeypatch, Unreachable("ok"))
    runs = run_bank_auto_syncs(factory, config=CONFIG, now=NOW)
    assert runs[0].status == STATUS_ERROR
    assert _entry(factory, entry_id).paused_reason is None

    class Rejected(FakeFinTSClient):
        def get_sepa_accounts(self):
            raise RuntimeError("9942 PIN falsch")

    _patch_client(monkeypatch, Rejected("ok"))
    runs = run_bank_auto_syncs(factory, config=CONFIG, now=NOW)
    assert runs[0].status == STATUS_ERROR and "angehalten" in runs[0].message
    assert _entry(factory, entry_id).paused_reason == PAUSE_BANK_ERROR
    assert run_bank_auto_syncs(factory, config=CONFIG, now=NOW) == []

    # Neu einschalten (neue PIN) setzt fort.
    _enable_fints(factory, seeded, pin="9999")
    assert _entry(factory, entry_id).paused_reason is None


def test_disable_and_connection_deactivation_delete_the_pin(factory, seeded):
    entry_id = _enable_fints(factory, seeded)
    with factory() as session:
        disable_bank_auto_sync(session, auto_sync_id=entry_id, changed_by="anna")
    entry = _entry(factory, entry_id)
    assert entry.is_active is False and entry.secret_encrypted is None

    _enable_fints(factory, seeded)
    with factory() as session:
        set_fints_connection_active(
            session=session, connection_id=seeded["fints"], is_active=False, changed_by="anna"
        )
    entry = _entry(factory, entry_id)
    assert entry.is_active is False and entry.secret_encrypted is None
    assert run_bank_auto_syncs(factory, config=CONFIG, now=NOW) == []


def test_one_failing_access_does_not_stop_the_others(factory, seeded, monkeypatch):
    first = _enable_fints(factory, seeded)
    with factory() as session:
        other = create_fints_connection(
            session=session, company_id=seeded["company"], bank_account_id=seeded["bank"],
            name="Volksbank", blz="10010010", login="kunde2",
            fints_url="https://fints.example.de/fints30", changed_by="anna",
        )
        second = enable_fints_auto_sync(
            session, connection_id=other.id, pin="1", secret_key=SECRET, user_id=None,
            changed_by="anna",
        ).id
    calls = []

    def boom_then_ok(connection, pin, product_id, from_data=None):
        calls.append(connection.name)
        if connection.name == "Sparkasse":
            raise ZeroDivisionError("kaputt")
        return FakeFinTSClient("ok")

    monkeypatch.setattr("app.services.fints_sync._build_client", boom_then_ok)
    runs = {run.auto_sync_id: run for run in run_bank_auto_syncs(factory, config=CONFIG, now=NOW)}
    assert runs[first].status == STATUS_ERROR and "Server-Log" in runs[first].message
    assert runs[second].status == STATUS_OK
    assert _entry(factory, second).user_id is None


# ---------------------------------------------------------------------------
# Open Banking
# ---------------------------------------------------------------------------


def _open_banking(factory, ids, *, authorized: bool) -> int:
    with factory() as session:
        connection = OpenBankingConnection(
            tenant_id=ids["tenant"],
            company_id=ids["company"],
            bank_account_id=ids["bank"],
            name="Finom PSD2",
            aspsp_name="Finom",
            aspsp_country="DE",
            session_id_encrypted=(
                open_banking._encrypt_session_id("sess-1", SECRET) if authorized else None
            ),
            account_uid="acc-1" if authorized else None,
            consent_valid_until=datetime(2027, 5, 1, tzinfo=timezone.utc),
        )
        session.add(connection)
        session.commit()
        return enable_open_banking_auto_sync(
            session, connection_id=connection.id, user_id=ids["user"], changed_by="anna"
        ).id


def test_open_banking_run_imports_and_reports_missing_consent(factory, seeded, monkeypatch):
    from test_open_banking import CONFIG as OB_CONFIG
    from test_open_banking import FakeEnableBanking

    monkeypatch.setattr(open_banking, "_build_client", lambda config: FakeEnableBanking())
    monkeypatch.setattr(bank_auto_sync, "open_banking_config", lambda *a, **k: OB_CONFIG)
    entry_id = _open_banking(factory, seeded, authorized=True)
    runs = run_bank_auto_syncs(factory, config=CONFIG, auto_sync_id=entry_id, now=NOW)
    assert runs[0].status == STATUS_OK and runs[0].imported == 3

    missing = _open_banking_second(factory, seeded)
    runs = run_bank_auto_syncs(factory, config=CONFIG, auto_sync_id=missing, now=NOW)
    assert runs[0].status == STATUS_ERROR and "Zustimmung" in runs[0].message
    # Kein Anhalten: Nach erneuerter Zustimmung läuft der Abruf von selbst weiter.
    assert _entry(factory, missing).paused_reason is None


def _open_banking_second(factory, ids) -> int:
    with factory() as session:
        bank = Account(
            tenant_id=ids["tenant"], company_id=ids["company"], code="1210", name="Bank 2",
            account_type="asset",
        )
        session.add(bank)
        session.commit()
        ids = {**ids, "bank": bank.id}
    with factory() as session:
        connection = OpenBankingConnection(
            tenant_id=ids["tenant"], company_id=ids["company"], bank_account_id=ids["bank"],
            name="Ohne Zustimmung", aspsp_name="Finom", aspsp_country="DE",
        )
        session.add(connection)
        session.commit()
        return enable_open_banking_auto_sync(
            session, connection_id=connection.id, user_id=None, changed_by="anna"
        ).id


# ---------------------------------------------------------------------------
# Bank-MCP-Server
# ---------------------------------------------------------------------------


def _mcp_server(factory, ids) -> int:
    with factory() as session:
        server = McpServer(
            tenant_id=ids["tenant"], name="Finom", tool_prefix="finom",
            url="https://bank.example/mcp", auth_type="none", credential_scope="global",
            tools_cache=FINOM_TOOLS, created_by="test",
        )
        session.add(server)
        session.commit()
        return server.id


def test_mcp_wallet_is_checked_and_synced_with_the_enabling_user(factory, seeded, monkeypatch):
    server_id = _mcp_server(factory, seeded)
    today = NOW.date().isoformat()
    fake = FakeFinom([_tx(f"{today}T07:00:00", 50.0, "Incoming", name="Kunde AG")])

    def caller(tool, arguments):
        return mcp_bank_sync._result_json(fake.call_remote(server_id, tool, arguments), tool)

    with factory() as session:
        company = session.get(Company, seeded["company"])
        with pytest.raises(BankAutoSyncError, match="Wallet"):
            enable_mcp_auto_sync(
                session, company=company, caller=caller, server_id=server_id,
                bank_account_id=seeded["bank"], wallet_id="gibt-es-nicht",
                user_id=seeded["user"], changed_by="anna",
            )
        entry_id = enable_mcp_auto_sync(
            session, company=company, caller=caller, server_id=server_id,
            bank_account_id=seeded["bank"], wallet_id=WALLET["id"],
            user_id=seeded["user"], changed_by="anna",
        ).id

    used = []

    class Caller:
        def __init__(self, user):
            used.append(user)

        def __enter__(self):
            return caller

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        bank_auto_sync, "_mcp_caller", lambda factory_, entry, user: Caller(user)
    )
    runs = run_bank_auto_syncs(factory, config=CONFIG, now=NOW)
    assert [(run.status, run.imported) for run in runs] == [(STATUS_OK, 1)]
    assert used[0]["id"] == seeded["user"] and used[0]["username"] == "anna"
    assert _entry(factory, entry_id).mcp_wallet_name == "Hauptkonto"

    with factory() as session:
        session.get(User, seeded["user"]).is_active = False
        session.commit()
    runs = run_bank_auto_syncs(factory, config=CONFIG, now=NOW)
    assert runs[0].status == STATUS_ERROR and "deaktiviert" in runs[0].message


# ---------------------------------------------------------------------------
# API, UI, Demo
# ---------------------------------------------------------------------------


@pytest.fixture()
def app_ids(tmp_path):
    app = create_app(
        {
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'auto_api.sqlite'}",
            "TESTING": True,
            "SECRET_KEY": SECRET,
            "API_REQUIRE_AUTH": False,
            "CSRF_PROTECT": False,
            "FINTS_PRODUCT_ID": PRODUCT_ID,
        }
    )
    with app.extensions["db_session_factory"]() as session:
        tenant = Tenant(name="API")
        company = Company(tenant=tenant, name="API GmbH", currency_code="EUR")
        session.add_all([tenant, company])
        session.flush()
        bank = Account(
            tenant_id=tenant.id, company_id=company.id, code="1200", name="Bank",
            account_type="asset",
        )
        session.add_all(
            [
                bank,
                User(
                    tenant_id=tenant.id, username="buchi", role=ROLE_BUCHHALTER,
                    password_hash=hash_password("geheim123"),
                ),
            ]
        )
        session.commit()
        connection = create_fints_connection(
            session=session, company_id=company.id, bank_account_id=bank.id, name="Sparkasse",
            blz="10010010", login="kunde1", fints_url="https://fints.example.de/fints30",
            changed_by="test",
        )
        ids = {"company": company.id, "bank": bank.id, "fints": connection.id}
    return app, ids


def test_api_enable_list_run_and_disable(app_ids, monkeypatch):
    app, ids = app_ids
    _patch_client(monkeypatch, FakeFinTSClient("ok"))
    client = app.test_client()

    missing = client.post(f"/api/v1/fints-connections/{ids['fints']}/auto-sync", json={})
    assert missing.status_code == 422
    enabled = client.post(
        f"/api/v1/fints-connections/{ids['fints']}/auto-sync", json={"pin": "1234"}
    )
    assert enabled.status_code == 200, enabled.get_json()
    body = enabled.get_json()
    assert body["source"] == "fints" and body["pin_stored"] is True and "1234" not in str(body)

    listing = client.get("/api/v1/bank-auto-syncs", query_string={"company_id": ids["company"]})
    assert listing.status_code == 200
    assert listing.get_json()["daily_at"] == "06:00"
    assert [item["id"] for item in listing.get_json()["auto_syncs"]] == [body["id"]]

    run = client.post("/api/v1/bank-auto-syncs/run", json={"company_id": ids["company"]})
    assert run.status_code == 200
    assert run.get_json()["runs"][0]["imported"] == 2

    disabled = client.post(f"/api/v1/bank-auto-syncs/{body['id']}/disable", json={})
    assert disabled.status_code == 200 and disabled.get_json()["is_active"] is False
    listing = client.get("/api/v1/bank-auto-syncs", query_string={"company_id": ids["company"]})
    assert listing.get_json()["auto_syncs"] == []

    assert client.get("/api/v1/bank-auto-syncs").status_code == 400
    bad = client.post(
        "/api/v1/bank-auto-syncs/run", json={"company_id": ids["company"], "auto_sync_id": "x"}
    )
    assert bad.status_code == 400
    assert (
        client.post(f"/api/v1/bank-accounts/{ids['bank']}/mcp-auto-sync", json={}).status_code
        == 400
    )


def test_manual_api_sync_resumes_a_paused_auto_sync(app_ids, monkeypatch):
    app, ids = app_ids
    client = app.test_client()
    client.post(f"/api/v1/fints-connections/{ids['fints']}/auto-sync", json={"pin": "1234"})
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(select(BankAutoSync)).scalar_one()
        entry.paused_reason = PAUSE_TAN_REQUIRED
        session.commit()
    _patch_client(monkeypatch, FakeFinTSClient("ok"))
    sync = client.post(f"/api/v1/fints-connections/{ids['fints']}/sync", json={"pin": "4321"})
    assert sync.status_code == 200
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(select(BankAutoSync)).scalar_one()
        assert entry.paused_reason is None
        assert bank_auto_sync._decrypt_pin(entry.secret_encrypted, SECRET) == "4321"


def test_bank_page_switches_auto_sync_on_and_off(app_ids, monkeypatch):
    app, ids = app_ids
    client = app.test_client()
    client.post("/auth/login", data={"username": "buchi", "password": "geheim123"})

    page = client.get("/bank", query_string={"company_id": ids["company"]})
    text = page.get_data(as_text=True)
    assert "Täglicher automatischer Abruf" in text and "Für keinen Zugang eingeschaltet" in text

    response = client.post(f"/bank/fints/{ids['fints']}/automatik", data={"pin": "1234"})
    assert response.status_code == 302
    text = client.get("/bank", query_string={"company_id": ids["company"]}).get_data(as_text=True)
    assert "täglich ab 06:00" in text and "noch nicht gelaufen" in text

    _patch_client(monkeypatch, FakeFinTSClient("ok"))
    response = client.post(
        "/bank/automatik/abrufen", data={"company_id": ids["company"]}, follow_redirects=True
    )
    assert "2 neu" in response.get_data(as_text=True)

    with app.extensions["db_session_factory"]() as session:
        entry_id = session.execute(select(BankAutoSync.id)).scalar_one()
    client.post(f"/bank/automatik/{entry_id}/ausschalten")
    with app.extensions["db_session_factory"]() as session:
        entry = session.get(BankAutoSync, entry_id)
        assert entry.is_active is False and entry.secret_encrypted is None


def test_demo_blocks_auto_sync_routes():
    assert demo_block_reason("/api/v1/bank-auto-syncs/run", "POST")
    assert demo_block_reason("/bank/automatik/abrufen", "POST")
    assert demo_block_reason("/bank/fints/1/automatik", "POST")
    assert demo_block_reason("/api/v1/open-banking-connections/1/auto-sync", "POST")


def test_cli_runs_once(app_ids, monkeypatch):
    app, ids = app_ids
    client = app.test_client()
    client.post(f"/api/v1/fints-connections/{ids['fints']}/auto-sync", json={"pin": "1234"})
    _patch_client(monkeypatch, FakeFinTSClient("ok"))
    result = app.test_cli_runner().invoke(args=["bank-auto-sync"])
    assert result.exit_code == 0, result.output
    assert "Sparkasse: ok – " in result.output and "2 neu" in result.output


def test_mcp_wallet_switch_on_the_web_page_and_api(bank_app):  # noqa: F811
    app, ids, _fake, _used = bank_app
    client = _login(app, "anna")
    page = client.get(
        "/bank/mcp-abruf", query_string={"company_id": ids["company"]}
    ).get_data(as_text=True)
    assert "Täglich automatisch abrufen" in page

    response = client.post(
        "/bank/mcp-abruf/automatik",
        data={
            "company_id": ids["company"],
            "server_id": ids["server"],
            "wallet_id": WALLET["id"],
            "bank_account_id": ids["bank"],
        },
    )
    assert response.status_code == 302
    page = client.get(
        "/bank/mcp-abruf", query_string={"company_id": ids["company"]}
    ).get_data(as_text=True)
    assert "Täglich automatisch nach 1230 – Bank Finom" in page

    api = app.test_client()
    rejected = api.post(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-auto-sync",
        json={"server_id": ids["server"], "wallet_id": "unbekannt"},
        headers=_auth("anna"),
    )
    assert rejected.status_code == 422
    listing = api.get(
        "/api/v1/bank-auto-syncs", query_string={"company_id": ids["company"]},
        headers=_auth("anna"),
    ).get_json()
    (item,) = listing["auto_syncs"]
    assert item["source"] == "mcp" and item["user"] == "anna" and item["pin_stored"] is None
    # Prüfer dürfen nicht einschalten.
    forbidden = api.post(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-auto-sync",
        json={"server_id": ids["server"], "wallet_id": WALLET["id"]},
        headers=_auth("paula"),
    )
    assert forbidden.status_code == 403
