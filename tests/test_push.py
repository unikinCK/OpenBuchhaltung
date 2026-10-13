"""Push-Mitteilungen: Geräteregistrierung, Ereignisse, Warteschlange und Dispatcher."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import select

from app import create_app
from app.auth import hash_api_token, hash_password
from app.services.bank_import import import_bank_csv
from app.services.elster import submit_vat_return
from app.services.open_items import OpenItemInput, create_open_item
from app.services.push import (
    DIGEST_TIMEZONE,
    EVENT_BANK_TRANSACTIONS,
    EVENT_OVERDUE_OPEN_ITEMS,
    MAX_ATTEMPTS,
    dispatch_pending,
    enqueue_overdue_digests,
    run_dispatch_cycle,
)
from app.services.vat_returns import save_vat_return
from domain.models import (
    Account,
    AuditLog,
    Company,
    PushDevice,
    PushNotification,
    Tenant,
    User,
)
from tests.test_vat_returns import _seed as _seed_vat_company
from tests.test_vat_returns import _seed_bookings

GLOBAL_TOKEN = "global-backend-token"
RELAY = "https://relay.test"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'push.db'}",
        "API_REQUIRE_AUTH": True,
        "API_AUTH_TOKEN": GLOBAL_TOKEN,
        "PUSH_RELAY_URL": RELAY,
        "PUSH_RELAY_TOKEN": "instanz-schluessel",
    }
    config.update(overrides)
    return create_app(config)


def _session(app):
    return app.extensions["db_session_factory"]()


def _seed(app) -> dict:
    """Zwei Mandanten mit je einer Gesellschaft; Benutzer maria (Mandant 1), otto
    (Mandant 2) und ein globaler Admin."""
    with _session(app) as session:
        tenant = Tenant(name="Push Tenant")
        other_tenant = Tenant(name="Fremder Tenant")
        company = Company(name="Push GmbH", currency_code="EUR", tenant=tenant)
        other_company = Company(name="Fremd GmbH", currency_code="EUR", tenant=other_tenant)
        session.add_all([tenant, other_tenant, company, other_company])
        session.flush()
        bank = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        receivable = Account(
            tenant_id=tenant.id,
            company_id=company.id,
            code="1400",
            name="Forderungen",
            account_type="asset",
        )
        session.add_all([bank, receivable])
        users = {}
        for username, role, tenant_id in (
            ("maria", "Buchhalter", tenant.id),
            ("otto", "Buchhalter", other_tenant.id),
            ("chef", "Admin", None),
        ):
            user = User(
                username=username,
                password_hash=hash_password("geheim123"),
                role=role,
                tenant_id=tenant_id,
                api_token_hash=hash_api_token(f"token-{username}"),
            )
            session.add(user)
            users[username] = user
        session.commit()
        return {
            "company_id": company.id,
            "other_company_id": other_company.id,
            "bank_id": bank.id,
            "receivable_id": receivable.id,
            **{f"{name}_id": user.id for name, user in users.items()},
        }


def _auth(username: str) -> dict:
    return {"Authorization": f"Bearer token-{username}"}


def _register(client, username: str, token: str = TOKEN_A, **payload):
    body = {
        "device_token": token,
        "environment": "sandbox",
        "device_name": f"iPhone {username}",
        "app_version": "1.0",
        "server_url": "https://obh.example",
        **payload,
    }
    return client.post("/api/v1/devices", json=body, headers=_auth(username))


class FakeRelay:
    def __init__(self, statuses: dict[int, str] | None = None, fail: bool = False):
        self.calls: list[tuple[str, str, dict]] = []
        self.statuses = statuses or {}
        self.fail = fail

    def __call__(self, url, token, payload, timeout):
        self.calls.append((url, token, payload))
        if self.fail:
            raise OSError("Relay down")
        return {
            "results": [
                {"id": item["id"], "status": self.statuses.get(item["id"], "sent")}
                for item in payload["notifications"]
            ]
        }


# --- Geräteregistrierung -------------------------------------------------------------


def test_register_device_and_status(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    client = app.test_client()

    status = client.get("/api/v1/push/status", headers=_auth("maria")).get_json()
    assert status["enabled"] is True
    assert {event["key"] for event in status["events"]} == {
        "bank_transactions",
        "elster_submission",
        "overdue_open_items",
    }

    response = _register(client, "maria", token=TOKEN_A.upper())
    assert response.status_code == 201, response.get_json()
    device = response.get_json()
    assert device["user_id"] == ids["maria_id"]
    assert device["token_suffix"] == TOKEN_A[-8:]
    assert device["environment"] == "sandbox"
    assert device["events"] == status["default_events"]
    assert device["server_url"] == "https://obh.example"
    assert "device_token" not in device

    with _session(app) as session:
        actions = session.execute(select(AuditLog.action)).scalars().all()
    assert "push_device_registered" in actions


@pytest.mark.parametrize(
    "payload",
    [
        {"device_token": "kein-token"},
        {"device_token": TOKEN_A, "environment": "staging"},
        {"device_token": TOKEN_A, "events": ["unbekannt"]},
        {"device_token": TOKEN_A, "events": "bank_transactions"},
        {"device_token": TOKEN_A, "platform": "android"},
    ],
)
def test_register_device_validates_input(tmp_path, payload):
    app = _create_app(tmp_path)
    _seed(app)
    response = app.test_client().post("/api/v1/devices", json=payload, headers=_auth("maria"))
    assert response.status_code == 400


def test_register_requires_user_token(tmp_path):
    app = _create_app(tmp_path)
    _seed(app)
    response = app.test_client().post(
        "/api/v1/devices",
        json={"device_token": TOKEN_A},
        headers={"Authorization": f"Bearer {GLOBAL_TOKEN}"},
    )
    assert response.status_code == 400


def test_reregistering_moves_device_to_new_user(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    client = app.test_client()
    first = _register(client, "maria").get_json()
    client.post(f"/api/v1/devices/{first['id']}/revoke", headers=_auth("maria"))

    second = _register(client, "otto", events=["bank_transactions"]).get_json()
    assert second["id"] == first["id"]
    assert second["user_id"] == ids["otto_id"]
    assert second["active"] is True
    assert second["events"] == ["bank_transactions"]
    assert client.get("/api/v1/devices", headers=_auth("maria")).get_json()["devices"] == []


def test_device_list_update_revoke_are_scoped(tmp_path):
    app = _create_app(tmp_path)
    _seed(app)
    client = app.test_client()
    maria_device = _register(client, "maria").get_json()
    _register(client, "otto", token=TOKEN_B)

    own = client.get("/api/v1/devices", headers=_auth("maria")).get_json()["devices"]
    assert [device["id"] for device in own] == [maria_device["id"]]
    everyone = client.get("/api/v1/devices", headers=_auth("chef")).get_json()["devices"]
    assert len(everyone) == 2

    path = f"/api/v1/devices/{maria_device['id']}"
    assert client.patch(path, json={"events": []}, headers=_auth("otto")).status_code == 404
    assert client.post(f"{path}/revoke", headers=_auth("otto")).status_code == 404

    updated = client.patch(
        path,
        json={"events": ["elster_submission"], "device_name": "Diensthandy"},
        headers=_auth("maria"),
    )
    assert updated.status_code == 200
    assert updated.get_json()["events"] == ["elster_submission"]
    assert updated.get_json()["device_name"] == "Diensthandy"
    bad = client.patch(path, json={"events": ["nix"]}, headers=_auth("maria"))
    assert bad.status_code == 400

    revoked = client.post(f"{path}/revoke", headers=_auth("maria"))
    assert revoked.get_json()["active"] is False
    assert client.patch(path, json={"events": []}, headers=_auth("maria")).status_code == 409
    assert client.get("/api/v1/devices", headers=_auth("maria")).get_json()["devices"] == []
    inactive = client.get(
        "/api/v1/devices?include_inactive=1", headers=_auth("maria")
    ).get_json()["devices"]
    assert [device["id"] for device in inactive] == [maria_device["id"]]


def test_test_notification_needs_relay(tmp_path):
    app = _create_app(tmp_path)
    _seed(app)
    client = app.test_client()
    device = _register(client, "maria").get_json()
    response = client.post(f"/api/v1/devices/{device['id']}/test", headers=_auth("maria"))
    assert response.status_code == 202

    (tmp_path / "ohne").mkdir()
    disabled = _create_app(tmp_path / "ohne", PUSH_RELAY_URL="")
    _seed(disabled)
    client = disabled.test_client()
    assert client.get("/api/v1/push/status", headers=_auth("maria")).get_json()["enabled"] is False
    device = _register(client, "maria").get_json()
    response = client.post(f"/api/v1/devices/{device['id']}/test", headers=_auth("maria"))
    assert response.status_code == 409


# --- Ereignisse ----------------------------------------------------------------------


def _import_bank(app, ids, rows: str) -> None:
    with app.app_context(), _session(app) as session:
        import_bank_csv(
            session=session,
            company_id=ids["company_id"],
            bank_account_id=ids["bank_id"],
            csv_stream=StringIO("Buchungstag;Verwendungszweck;Betrag\n" + rows),
            changed_by="pytest",
        )


def _notifications(app) -> list[PushNotification]:
    with _session(app) as session:
        rows = session.execute(select(PushNotification).order_by(PushNotification.id))
        return list(rows.scalars())


def test_bank_import_notifies_subscribed_devices_of_the_tenant(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    client = app.test_client()
    maria = _register(client, "maria").get_json()
    _register(client, "otto", token=TOKEN_B)  # anderer Mandant
    chef = _register(client, "chef", token="c" * 64, events=["elster_submission"]).get_json()

    _import_bank(app, ids, "05.07.2026;Miete;-595,00\n06.07.2026;Server;-49,00\n")
    rows = _notifications(app)
    assert [(row.device_id, row.event, row.body) for row in rows] == [
        (maria["id"], EVENT_BANK_TRANSACTIONS, "2 neue Bankumsätze")
    ]
    assert rows[0].company_id == ids["company_id"]
    assert chef["id"] not in {row.device_id for row in rows}

    _import_bank(app, ids, "05.07.2026;Miete;-595,00\n")  # nur Duplikat
    assert len(_notifications(app)) == 1


def test_no_notifications_without_relay(tmp_path):
    app = _create_app(tmp_path, PUSH_RELAY_URL="")
    ids = _seed(app)
    _register(app.test_client(), "maria")
    _import_bank(app, ids, "05.07.2026;Miete;-595,00\n")
    assert _notifications(app) == []


def test_elster_submission_notifies(tmp_path):
    app = _create_app(tmp_path)
    with app.app_context(), _session(app) as session:
        company = _seed_vat_company(session)
        _seed_bookings(session, company)
        user = User(
            username="steuer",
            password_hash=hash_password("geheim123"),
            role="Buchhalter",
            tenant_id=company.tenant_id,
            api_token_hash=hash_api_token("token-steuer"),
        )
        session.add(user)
        session.commit()
        company_id = company.id
    _register(app.test_client(), "steuer", events=["elster_submission"])

    with app.app_context(), _session(app) as session:
        vat_return = save_vat_return(
            session=session, company_id=company_id, period_label="2026-05", changed_by="pytest"
        )
        submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="test",
            transport="mock",
            changed_by="pytest",
        )
    [row] = _notifications(app)
    assert row.event == "elster_submission"
    assert row.body == "UStVA 2026-05 an ELSTER übermittelt"


def test_overdue_digest_once_per_day(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    _register(app.test_client(), "maria")
    today = datetime.now(DIGEST_TIMEZONE).date()
    with _session(app) as session:
        create_open_item(
            session=session,
            payload=OpenItemInput(
                company_id=ids["company_id"],
                account_id=ids["receivable_id"],
                item_type="receivable",
                reference="RE-1",
                counterparty="Kunde",
                entry_date=today - timedelta(days=40),
                due_date=today - timedelta(days=10),
                amount=Decimal("100.00"),
                changed_by="pytest",
            ),
        )
        session.commit()

    morning = datetime.combine(today, time(6), tzinfo=DIGEST_TIMEZONE)
    later = datetime.combine(today, time(10), tzinfo=DIGEST_TIMEZONE)
    relay = FakeRelay()
    config = app.config
    with _session(app) as session:
        assert run_dispatch_cycle(session, config=config, transport=relay, now=morning)[
            "digests"
        ] == 0
        assert run_dispatch_cycle(session, config=config, transport=relay, now=later)[
            "digests"
        ] == 1
        assert enqueue_overdue_digests(session, now=later, config=config) == 0
        next_day = later + timedelta(days=1)
        assert enqueue_overdue_digests(session, now=next_day, config=config) == 1
        session.rollback()
    rows = [row for row in _notifications(app) if row.event == EVENT_OVERDUE_OPEN_ITEMS]
    assert [row.body for row in rows] == ["1 überfälliger offener Posten"]
    assert rows[0].status == "sent"


# --- Dispatcher ----------------------------------------------------------------------


def _queue(app, ids, count: int = 1) -> None:
    rows = "".join(f"0{day}.07.2026;Umsatz {day};-{day},00\n" for day in range(1, count + 1))
    _import_bank(app, ids, rows)


def test_dispatch_sends_payload_to_relay(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    device = _register(app.test_client(), "maria").get_json()
    _queue(app, ids)
    relay = FakeRelay()
    now = datetime.now(timezone.utc)
    with _session(app) as session:
        counts = dispatch_pending(session, config=app.config, transport=relay, now=now)
    assert counts["sent"] == 1

    [(url, token, payload)] = relay.calls
    assert url == f"{RELAY}/v1/notifications"
    assert token == "instanz-schluessel"
    [item] = payload["notifications"]
    assert item["device_token"] == TOKEN_A
    assert item["environment"] == "sandbox"
    assert item["title"] == "OpenBuchhaltung"
    assert item["body"] == "1 neuer Bankumsatz"
    assert item["category"] == EVENT_BANK_TRANSACTIONS
    assert item["thread_id"] == f"company-{ids['company_id']}"
    assert item["data"] == {
        "event": EVENT_BANK_TRANSACTIONS,
        "company_id": ids["company_id"],
        "server_url": "https://obh.example",
    }
    [row] = _notifications(app)
    assert row.status == "sent" and row.sent_at is not None and row.device_id == device["id"]

    with _session(app) as session:
        assert dispatch_pending(session, config=app.config, transport=relay, now=now)["sent"] == 0
    assert len(relay.calls) == 1


def test_dispatch_retries_then_fails(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    _register(app.test_client(), "maria")
    _queue(app, ids)
    relay = FakeRelay(fail=True)
    now = datetime.now(timezone.utc)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with _session(app) as session:
            dispatch_pending(session, config=app.config, transport=relay, now=now)
        [row] = _notifications(app)
        assert row.attempts == attempt
    assert row.status == "failed"
    assert "OSError" in row.last_error
    assert "instanz-schluessel" not in row.last_error


def test_invalid_token_revokes_device(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    device = _register(app.test_client(), "maria").get_json()
    _queue(app, ids)
    _queue(app, ids, count=2)  # zweiter Import, eine weitere Mitteilung
    first, _second = _notifications(app)
    relay = FakeRelay(statuses={first.id: "invalid_token"})
    with _session(app) as session:
        dispatch_pending(
            session, config=app.config, transport=relay, now=datetime.now(timezone.utc)
        )
        stored = session.get(PushDevice, device["id"])
        assert stored.revoked_at is not None
    statuses = {row.id: row.status for row in _notifications(app)}
    assert statuses[first.id] == "failed"


def test_dispatch_discards_stale_and_revoked_and_purges_old(tmp_path):
    app = _create_app(tmp_path)
    ids = _seed(app)
    client = app.test_client()
    device = _register(client, "maria").get_json()
    _queue(app, ids)
    now = datetime.now(timezone.utc)
    relay = FakeRelay()

    with _session(app) as session:
        dispatch_pending(session, config=app.config, transport=relay, now=now + timedelta(days=2))
    [row] = _notifications(app)
    assert row.status == "discarded"

    with _session(app) as session:
        dispatch_pending(session, config=app.config, transport=relay, now=now + timedelta(days=31))
    assert _notifications(app) == []

    _queue(app, ids, count=2)
    client.post(f"/api/v1/devices/{device['id']}/revoke", headers=_auth("maria"))
    assert {row.status for row in _notifications(app)} == {"discarded"}
    assert relay.calls == []


# --- Weboberfläche und CLI -----------------------------------------------------------


def _ui_login(client, username: str = "maria"):
    response = client.post("/auth/login", data={"username": username, "password": "geheim123"})
    assert response.status_code == 302


def test_apps_page_manages_devices(tmp_path):
    app = _create_app(tmp_path)
    _seed(app)
    client = app.test_client()
    device = _register(client, "maria").get_json()
    _register(client, "otto", token=TOKEN_B)

    _ui_login(client)
    page = client.get("/auth/apps").get_data(as_text=True)
    assert "Mobilgeräte" in page and "iPhone maria" in page and "iPhone otto" not in page
    assert "Test senden" in page

    client.post(
        f"/auth/apps/geraete/{device['id']}/ereignisse",
        data={"events": ["bank_transactions", "unbekannt"]},
    )
    with _session(app) as session:
        assert session.get(PushDevice, device["id"]).events == ["bank_transactions"]

    assert client.post(f"/auth/apps/geraete/{device['id']}/test").status_code == 302
    assert [row.event for row in _notifications(app)] == ["test"]

    other = client.get("/api/v1/devices", headers=_auth("otto")).get_json()["devices"][0]
    assert client.post(f"/auth/apps/geraete/{other['id']}/widerrufen").status_code == 404
    client.post(f"/auth/apps/geraete/{device['id']}/widerrufen")
    with _session(app) as session:
        assert session.get(PushDevice, device["id"]).revoked_at is not None


def test_push_dispatch_cli(tmp_path, monkeypatch):
    app = _create_app(tmp_path)
    ids = _seed(app)
    _register(app.test_client(), "maria")
    _queue(app, ids)
    relay = FakeRelay()
    monkeypatch.setattr("app.services.push.http_relay_transport", relay)
    result = app.test_cli_runner().invoke(args=["push-dispatch", "--once"])
    assert result.exit_code == 0, result.output
    assert "sent=1" in result.output

    (tmp_path / "ohne").mkdir()
    disabled = _create_app(tmp_path / "ohne", PUSH_RELAY_URL="")
    result = disabled.test_cli_runner().invoke(args=["push-dispatch", "--once"])
    assert result.exit_code != 0
    assert "PUSH_RELAY_URL" in result.output
