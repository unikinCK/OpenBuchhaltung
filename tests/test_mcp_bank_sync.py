"""Bankumsätze über den MCP-Server der Bank (Finom) – ohne KI.

Der Fake verhält sich wie der Finom-MCP-Server: höchstens 10 Umsätze je Aufruf, neueste
zuerst, ``limitReached`` statt Cursor, Filter über ``dateFrom``/``dateBefore`` (UTC).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_mcp_connections import (  # noqa: F401 – fake_mcp ist eine Fixture
    _auth,
    _create_app,
    _create_server,
    _login,
    fake_mcp,
)

from app.services import mcp_bank_sync
from app.services.bank_statement import (
    BankStatementRow,
    BankStatementRowError,
    row_from_finom_mcp_transaction,
)
from app.services.mcp_bank_sync import (
    McpBankSyncError,
    default_sync_range,
    fetch_transactions,
    sync_mcp_bank_transactions,
)
from app.services.mcp_client import McpToolResult
from app.services.mcp_connections import McpConnectionError, server_toolbox
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    McpServer,
    Tenant,
)

WALLET = {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "Hauptkonto",
    "currency": "EUR",
    "iban": "DE89370400440532013000",
    "isDefault": True,
    "isClosed": False,
}
TAX_WALLET = {
    "id": "22222222-2222-4222-8222-222222222222",
    "name": "Steuerrücklage",
    "currency": "EUR",
    "iban": "DE02120300000000202051",
    "isDefault": False,
    "isClosed": False,
}
FINOM_TOOLS = [
    {"name": "transactions__listWallets", "annotations": {"readOnlyHint": True}},
    {"name": "transactions__listTransactions", "annotations": {"readOnlyHint": True}},
]


def _tx(
    executed: str,
    amount: float,
    direction: str = "Outgoing",
    *,
    wallet: str = WALLET["id"],
    status: str = "Completed",
    payment_type: str = "Payment",
    description: str | None = "Rechnung 42",
    name: str | None = "Lieferant GmbH",
    tx_id: str | None = None,
) -> dict:
    return {
        "id": tx_id or str(uuid.uuid4()),
        "executed": executed,
        "created": executed,
        "amount": amount,
        "currency": "EUR",
        "direction": direction,
        "status": status,
        "paymentType": payment_type,
        "description": description,
        "accountId": wallet,
        "participantName": name,
        "participantIban": "DE02120300000000202051" if name else None,
    }


def _moment(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class FakeFinom:
    """Toolbox-Ersatz mit dem Verhalten des Finom-MCP-Servers."""

    PAGE_CAP = 10

    def __init__(self, transactions: list[dict], wallets: list[dict] | None = None):
        self.transactions = transactions
        self.wallets = wallets if wallets is not None else [WALLET, TAX_WALLET]
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def call_remote(self, server_id, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "transactions__listWallets":
            return McpToolResult(
                content=[{"type": "text", "text": "…"}],
                structured_content={"wallets": self.wallets},
            )
        assert name == "transactions__listTransactions"
        lower = _moment(arguments["dateFrom"]) if arguments.get("dateFrom") else None
        before = _moment(arguments["dateBefore"]) if arguments.get("dateBefore") else None
        matching = [
            item
            for item in self.transactions
            if item["accountId"] in (arguments.get("accountIds") or [item["accountId"]])
            and item["status"] in (arguments.get("statuses") or [item["status"]])
            and (lower is None or _moment(item["executed"]) >= lower)
            and (before is None or _moment(item["executed"]) < before)
        ]
        matching.sort(key=lambda item: _moment(item["executed"]), reverse=True)
        limit = min(arguments.get("limit") or self.PAGE_CAP, self.PAGE_CAP)
        return McpToolResult(
            content=[{"type": "text", "text": "…"}],
            structured_content={
                "transactions": matching[:limit],
                "limitReached": len(matching) > limit,
                "limit": limit,
            },
        )

    def close(self):
        self.closed = True


def _caller(fake: FakeFinom):
    return lambda tool, arguments: mcp_bank_sync._result_json(
        fake.call_remote(1, tool, arguments), tool
    )


# ---------------------------------------------------------------------------
# Zuordnung eines Umsatzes
# ---------------------------------------------------------------------------


def test_row_mapping_signs_amount_and_builds_purpose_like_the_csv_import():
    incoming = row_from_finom_mcp_transaction(
        1, _tx("2026-11-15T10:17:28.310534", 18143.0, "Incoming", name="Kunde AG", tx_id="a-1")
    )
    assert isinstance(incoming, BankStatementRow)
    assert incoming.amount == Decimal("18143.00")
    assert incoming.booking_date == date(2026, 11, 15)
    assert (incoming.purpose, incoming.counterparty) == ("Rechnung 42", "Kunde AG")
    assert incoming.bank_reference == "a-1"
    assert incoming.counterparty_iban == "DE02120300000000202051"
    assert incoming.currency_code == "EUR"

    card = row_from_finom_mcp_transaction(
        2, _tx("2026-11-14T08:00:00", 12.5, payment_type="CardPayment", name="Bäckerei")
    )
    assert card.amount == Decimal("-12.50")
    assert card.purpose == "Kartenzahlung Bäckerei"

    fee = row_from_finom_mcp_transaction(
        3, _tx("2026-11-14T08:00:00", 9.0, payment_type="Fee", description="", name="Finom")
    )
    assert fee.purpose == "Fee Finom"


def test_row_mapping_uses_the_berlin_day_and_rejects_unclear_rows():
    # 22:30 UTC am 31.3. ist in Berlin (Sommerzeit) schon der 1.4.
    late = row_from_finom_mcp_transaction(1, _tx("2026-03-31T22:30:00Z", 5.0))
    assert late.booking_date == date(2026, 4, 1)
    winter = row_from_finom_mcp_transaction(1, _tx("2026-01-15T22:30:00", 5.0))
    assert winter.booking_date == date(2026, 1, 15)

    pending = row_from_finom_mcp_transaction(1, _tx("2026-11-15T10:00:00", 5.0, status="ToPay"))
    assert isinstance(pending, BankStatementRowError)
    unknown = row_from_finom_mcp_transaction(1, _tx("2026-11-15T10:00:00", 5.0, direction="?"))
    assert isinstance(unknown, BankStatementRowError)
    no_time = row_from_finom_mcp_transaction(1, {**_tx("x", 5.0), "executed": None, "created": ""})
    assert isinstance(no_time, BankStatementRowError)


# ---------------------------------------------------------------------------
# Blättern
# ---------------------------------------------------------------------------


def test_fetch_pages_backwards_through_the_window_without_losing_ties():
    base = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    transactions = [
        _tx((base + timedelta(hours=index)).isoformat(), 10 + index) for index in range(23)
    ]
    # Drei Umsätze mit demselben Zeitpunkt genau an einer Seitengrenze.
    tie = (base + timedelta(hours=12)).isoformat()
    transactions += [_tx(tie, 99), _tx(tie, 98)]
    transactions.append(_tx("2026-10-01T09:00:00", 1.0, wallet=TAX_WALLET["id"]))
    transactions.append(_tx("2026-10-01T09:30:00", 1.0, status="Declined"))
    fake = FakeFinom(transactions)

    fetched = fetch_transactions(
        _caller(fake),
        wallet_id=WALLET["id"],
        from_date=date(2026, 10, 1),
        to_date=date(2026, 10, 31),
    )

    assert sorted(item["id"] for item in fetched) == sorted(
        item["id"] for item in transactions[:25]
    )
    first = fake.calls[0][1]
    assert first["accountIds"] == [WALLET["id"]] and first["statuses"] == ["Completed"]
    # Tage gelten in Europe/Berlin: 1.10. 00:00 MESZ = 30.9. 22:00 UTC.
    assert first["dateFrom"] == "2026-09-30T22:00:00.000000Z"
    assert first["dateBefore"] == "2026-10-31T23:00:00.000000Z"
    assert len(fake.calls) >= 3


def test_fetch_aborts_when_the_server_does_not_get_further():
    same = "2026-10-05T12:00:00"
    fake = FakeFinom([_tx(same, 1 + index) for index in range(11)])
    with pytest.raises(McpBankSyncError, match="keine weiteren Umsätze"):
        fetch_transactions(
            _caller(fake),
            wallet_id=WALLET["id"],
            from_date=date(2026, 10, 1),
            to_date=date(2026, 10, 31),
        )


# ---------------------------------------------------------------------------
# Zeitraum und Import
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> tuple[Company, Account, McpServer]:
    tenant = Tenant(name="Bank-MCP")
    company = Company(tenant=tenant, name="Bank-MCP GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    bank = Account(
        tenant_id=tenant.id, company_id=company.id, code="1230", name="Bank", account_type="asset"
    )
    server = McpServer(
        tenant_id=tenant.id,
        name="Finom",
        tool_prefix="finom",
        url="https://bank.example/mcp",
        auth_type="none",
        credential_scope="global",
        tools_cache=FINOM_TOOLS,
        created_by="test",
    )
    session.add_all([bank, server])
    session.commit()
    return company, bank, server


def _existing(session, company, bank, booking_date: date, reference: str | None, amount="1.00"):
    session.add(
        BankTransaction(
            tenant_id=company.tenant_id,
            company_id=company.id,
            bank_account_id=bank.id,
            booking_date=booking_date,
            amount=Decimal(amount),
            currency_code="EUR",
            purpose="Altbestand",
            dedup_hash=uuid.uuid4().hex,
            bank_reference=reference,
        )
    )
    session.commit()


def test_default_range_respects_rows_that_cannot_be_deduplicated(session):
    company, bank, _ = _seed(session)
    today = date(2026, 11, 15)
    kwargs = dict(company_id=company.id, bank_account_id=bank.id, today=today)
    assert default_sync_range(session, **kwargs) == (date(2026, 8, 17), today)

    _existing(session, company, bank, date(2026, 10, 9), None)
    assert default_sync_range(session, **kwargs) == (date(2026, 10, 10), today)

    _existing(session, company, bank, date(2026, 11, 1), str(uuid.uuid4()))
    assert default_sync_range(session, **kwargs) == (date(2026, 10, 25), today)

    # Ein späterer Umsatz ohne Finom-Referenz (z. B. CAMT) begrenzt die Überlappung.
    _existing(session, company, bank, date(2026, 10, 28), "CAMT-REF-1")
    assert default_sync_range(session, **kwargs) == (date(2026, 10, 29), today)


def test_sync_imports_new_rows_skips_known_ids_and_remembers_the_wallet(session):
    company, bank, server = _seed(session)
    known = str(uuid.uuid4())
    _existing(session, company, bank, date(2026, 11, 2), known, amount="-5.00")
    fake = FakeFinom(
        [
            _tx("2026-11-02T08:00:00", 5.0, tx_id=known, description="anderer Text"),
            _tx("2026-11-03T08:00:00", 100.0, "Incoming", name="Kunde AG"),
            _tx("2026-11-04T08:00:00", 20.0, payment_type="CardPayment", name="Tankstelle"),
        ]
    )

    with pytest.raises(McpBankSyncError, match="Wallet wählen"):
        # Zwei offene Wallets, keine Standard-Wallet und noch kein Abruf: nicht raten.
        sync_mcp_bank_transactions(
            session=session,
            caller=_caller(FakeFinom([], wallets=[{**WALLET, "isDefault": False}, TAX_WALLET])),
            company=company,
            server=server,
            bank_account_id=bank.id,
            changed_by="test",
        )

    preview = sync_mcp_bank_transactions(
        session=session,
        caller=_caller(fake),
        company=company,
        server=server,
        bank_account_id=bank.id,
        wallet_id=WALLET["id"],
        from_date=date(2026, 11, 1),
        to_date=date(2026, 11, 15),
        dry_run=True,
        changed_by="test",
    )
    assert (preview.fetched, preview.known, len(preview.new_rows)) == (3, 1, 2)
    assert preview.to_dict()["rows"][0]["purpose"] == "Rechnung 42"
    assert session.execute(select(BankTransaction)).scalars().all().__len__() == 1

    result = sync_mcp_bank_transactions(
        session=session,
        caller=_caller(fake),
        company=company,
        server=server,
        bank_account_id=bank.id,
        wallet_id=WALLET["id"],
        from_date=date(2026, 11, 1),
        to_date=date(2026, 11, 15),
        changed_by="test",
    )
    assert result.report.imported_rows == 2
    rows = session.execute(
        select(BankTransaction).order_by(BankTransaction.booking_date)
    ).scalars().all()
    assert [str(row.amount) for row in rows] == ["-5.00", "100.00", "-20.00"]
    assert rows[2].purpose == "Kartenzahlung Tankstelle"
    entry = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "bank_mcp_sync")
    ).scalar_one()
    assert entry.payload["wallet_id"] == WALLET["id"] and entry.payload["imported"] == 2

    # Zweiter Lauf ohne Wallet und Zeitraum: zuletzt genutzte Wallet, nichts doppelt.
    again = sync_mcp_bank_transactions(
        session=session,
        caller=_caller(fake),
        company=company,
        server=server,
        bank_account_id=bank.id,
        changed_by="test",
    )
    assert again.wallet.id == WALLET["id"]
    assert again.report.imported_rows == 0 and again.known == 3
    assert again.from_date == date(2026, 10, 28)


def test_sync_rejects_foreign_accounts_and_unknown_wallets(session):
    company, bank, server = _seed(session)
    receivables = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1400",
        name="Forderungen",
        account_type="asset",
        subledger="debtor",
    )
    session.add(receivables)
    session.commit()
    for account_id, wallet_id, message in (
        (receivables.id, WALLET["id"], "Bankkonto nicht gefunden"),
        (bank.id, "unbekannt", "Wallet gibt es"),
    ):
        with pytest.raises(McpBankSyncError, match=message):
            sync_mcp_bank_transactions(
                session=session,
                caller=_caller(FakeFinom([])),
                company=company,
                server=server,
                bank_account_id=account_id,
                wallet_id=wallet_id,
                changed_by="test",
            )


def test_server_needs_both_finom_tools_released_for_obi(session):
    company, _, server = _seed(session)
    assert mcp_bank_sync.bank_adapter(server) == "finom"
    server.allowed_tools = ["transactions__listTransactions"]
    assert mcp_bank_sync.bank_adapter(server) is None
    assert mcp_bank_sync.bank_sync_servers(session, tenant_id=company.tenant_id) == []
    with pytest.raises(McpBankSyncError, match="keinen Umsatzabruf"):
        mcp_bank_sync.resolve_server(session, company, server.id)


# ---------------------------------------------------------------------------
# REST, MCP-Werkzeug und Weboberfläche
# ---------------------------------------------------------------------------


@pytest.fixture()
def bank_app(tmp_path, monkeypatch):
    app = _create_app(tmp_path)
    company_id = app.config["_TEST_COMPANY_ID"]
    tenant_id = app.config["_TEST_TENANT_ID"]
    with app.extensions["db_session_factory"]() as db:
        bank = Account(
            tenant_id=tenant_id, company_id=company_id, code="1230", name="Bank Finom",
            account_type="asset",
        )
        server = McpServer(
            tenant_id=tenant_id,
            name="Finom",
            tool_prefix="finom",
            url="https://bank.example/mcp",
            auth_type="none",
            credential_scope="global",
            tools_cache=FINOM_TOOLS,
            created_by="test",
        )
        db.add_all([bank, server])
        db.commit()
        ids = {"company": company_id, "bank": bank.id, "server": server.id}
    fake = FakeFinom(
        [
            _tx("2026-11-03T08:00:00", 100.0, "Incoming", name="Kunde AG"),
            _tx("2026-11-04T08:00:00", 20.0, payment_type="CardPayment", name="Tankstelle"),
        ]
    )
    used: list[dict] = []

    def fake_toolbox(**kwargs):
        used.append(kwargs)
        return fake

    monkeypatch.setattr(mcp_bank_sync, "server_toolbox", fake_toolbox)
    return app, ids, fake, used


def test_api_lists_sources_wallets_previews_and_imports(bank_app):
    app, ids, fake, used = bank_app
    client = app.test_client()
    headers = _auth("anna")

    sources = client.get(
        "/api/v1/mcp-bank-sources", query_string={"company_id": ids["company"]}, headers=headers
    )
    assert sources.status_code == 200, sources.get_json()
    assert sources.get_json()["sources"] == [
        {"server_id": ids["server"], "name": "Finom", "adapter": "finom"}
    ]

    wallets = client.get(
        f"/api/v1/mcp-servers/{ids['server']}/bank-wallets",
        query_string={"company_id": ids["company"], "bank_account_id": ids["bank"]},
        headers=headers,
    )
    assert wallets.status_code == 200, wallets.get_json()
    body = wallets.get_json()
    assert [wallet["id"] for wallet in body["wallets"]] == [WALLET["id"], TAX_WALLET["id"]]
    assert body["suggested_wallet_id"] == WALLET["id"]
    assert used[-1]["user"]["username"] == "anna"

    preview = client.get(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-sync/preview",
        query_string={
            "server_id": ids["server"],
            "wallet_id": WALLET["id"],
            "from_date": "2026-11-01",
            "to_date": "2026-11-15",
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.get_json()
    assert preview.get_json()["status"] == "preview" and preview.get_json()["new_rows"] == 2

    synced = client.post(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-sync",
        json={"server_id": ids["server"], "from_date": "2026-11-01", "to_date": "2026-11-15"},
        headers=headers,
    )
    assert synced.status_code == 200, synced.get_json()
    assert synced.get_json()["report"]["imported_rows"] == 2
    assert fake.closed

    bad = client.post(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-sync",
        json={"server_id": ids["server"], "from_date": "gestern"},
        headers=headers,
    )
    assert bad.status_code == 422
    assert client.post(
        f"/api/v1/bank-accounts/{ids['bank']}/mcp-sync", json={}, headers=headers
    ).status_code == 400
    # Prüfer dürfen Bankdaten des Anbieters nicht abrufen.
    assert client.get(
        "/api/v1/mcp-bank-sources", query_string={"company_id": ids["company"]},
        headers=_auth("paula"),
    ).status_code == 403


def test_mcp_tools_reach_the_endpoints(bank_app):
    app, ids, _fake, _used = bank_app
    response = app.test_client().post(
        "/api/v1/mcp/call",
        headers=_auth("anna"),
        json={
            "method": "tools/call",
            "params": {
                "name": "preview_mcp_bank_transactions",
                "arguments": {
                    "bank_account_id": ids["bank"],
                    "server_id": ids["server"],
                    "wallet_id": WALLET["id"],
                    "from_date": "2026-11-01",
                },
            },
        },
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    result = response.get_json()["result"]
    assert result.get("isError") is not True, result
    assert '"new_rows": 2' in result["content"][0]["text"].replace("\n", " ") or (
        '"new_rows":2' in result["content"][0]["text"]
    )


def test_web_page_shows_wallets_preview_and_imports(bank_app):
    app, ids, _fake, _used = bank_app
    client = _login(app, "anna")

    bank_page = client.get(f"/bank?company_id={ids['company']}").get_data(as_text=True)
    assert "Umsätze von Finom abrufen" in bank_page

    page = client.get(
        "/bank/mcp-abruf", query_string={"company_id": ids["company"]}
    ).get_data(as_text=True)
    assert "Hauptkonto" in page and "Steuerrücklage" in page

    preview = client.get(
        "/bank/mcp-abruf",
        query_string={
            "company_id": ids["company"],
            "server_id": ids["server"],
            "wallet_id": WALLET["id"],
            "bank_account_id": ids["bank"],
            "from_date": "2026-11-01",
            "to_date": "2026-11-15",
        },
    ).get_data(as_text=True)
    assert "2 neu" in preview and "Kartenzahlung Tankstelle" in preview
    assert "2 Umsätze übernehmen" in preview

    imported = client.post(
        "/bank/mcp-abruf/uebernehmen",
        data={
            "company_id": ids["company"],
            "server_id": ids["server"],
            "wallet_id": WALLET["id"],
            "bank_account_id": ids["bank"],
            "from_date": "2026-11-01",
            "to_date": "2026-11-15",
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "2 neu" in imported
    with app.extensions["db_session_factory"]() as db:
        assert len(db.execute(select(BankTransaction)).scalars().all()) == 2


# ---------------------------------------------------------------------------
# Werkzeugkasten eines Servers (echter MCP-Client gegen den Fake-Server)
# ---------------------------------------------------------------------------


def test_server_toolbox_calls_by_remote_name_with_the_effective_login(
    tmp_path, fake_mcp  # noqa: F811
):
    app = _create_app(tmp_path)
    fake_mcp.state.update(auth_header="Authorization", required_auth="Bearer zentral-1")
    server_id = _create_server(app, fake_mcp, auth_type="token", credential_scope="global")
    app.test_client().post(
        f"/api/v1/mcp-servers/{server_id}/credential",
        json={"token": "zentral-1"},
        headers=_auth("admin"),
    )
    app.test_client().post(f"/api/v1/mcp-servers/{server_id}/tools/refresh", headers=_auth("admin"))
    factory = app.extensions["db_session_factory"]
    tenant_id = app.config["_TEST_TENANT_ID"]
    with app.app_context():
        toolbox = server_toolbox(
            session_factory=factory,
            server_id=server_id,
            tenant_id=tenant_id,
            company_id=app.config["_TEST_COMPANY_ID"],
            user={"id": 1, "username": "anna", "role": "Buchhalter", "tenant_id": tenant_id},
        )
        try:
            result = toolbox.call_remote(server_id, "search_documents", {"query": "x"})
            assert result.is_error is False
            assert fake_mcp.state["calls"][-1]["auth"] == "Bearer zentral-1"
            with pytest.raises(McpConnectionError, match="nicht verfügbar"):
                toolbox.call_remote(server_id, "gibt_es_nicht", {})
        finally:
            toolbox.close()
        # Ohne eigene Anmeldung bei einem Server mit Zugang je Benutzer: Hinweis statt Aufruf.
        with factory() as db:
            db.get(McpServer, server_id).credential_scope = "user"
            db.commit()
        with pytest.raises(McpConnectionError, match="nicht angemeldet"):
            server_toolbox(
                session_factory=factory,
                server_id=server_id,
                tenant_id=tenant_id,
                company_id=None,
                user={"id": 1, "username": "anna", "role": "Buchhalter", "tenant_id": tenant_id},
            )
        with factory() as db:
            db.get(McpServer, server_id).is_active = False
            db.commit()
        with pytest.raises(McpConnectionError, match="nicht \\(mehr\\) verfügbar"):
            server_toolbox(
                session_factory=factory,
                server_id=server_id,
                tenant_id=tenant_id,
                company_id=None,
                user=None,
            )
