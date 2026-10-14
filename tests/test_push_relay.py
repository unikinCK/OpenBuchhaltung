"""Push-Relay: Protokoll v1, Instanzschlüssel, Rate-Limit, APNs-Zustellung, Ende-zu-Ende."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from io import StringIO

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.services.bank_import import import_bank_csv
from app.services.push import dispatch_pending
from push_relay.__main__ import main as relay_cli
from push_relay.apns import (
    ApnsSender,
    DryRunSender,
    Notification,
    load_private_key,
    provider_token,
)
from push_relay.app import (
    ConfigError,
    RateLimiter,
    create_app,
    hash_instance_key,
    parse_instance_keys,
)
from tests.test_push import TOKEN_A, _create_app, _register, _seed

KEY = "instanz-schluessel"
TOKEN = "f" * 64


def _relay(sender=None, **config):
    settings = {"RELAY_INSTANCE_KEYS": f"demo:{hash_instance_key(KEY)}", **config}
    return create_app(settings, sender=sender or DryRunSender())


def _item(item_id: int = 1, **overrides) -> dict:
    item = {
        "id": item_id,
        "device_token": TOKEN,
        "environment": "sandbox",
        "title": "OpenBuchhaltung",
        "body": "3 neue Bankumsätze",
        "category": "bank_transactions",
        "thread_id": "company-1",
        "data": {"event": "bank_transactions", "company_id": 1},
    }
    item.update(overrides)
    return item


def _post(client, items, key: str = KEY):
    return client.post(
        "/v1/notifications",
        json={"notifications": items},
        headers={"Authorization": f"Bearer {key}"},
    )


def _private_key():
    return ec.generate_private_key(ec.SECP256R1())


def _notification(**overrides) -> Notification:
    values = {
        "id": 7,
        "device_token": TOKEN,
        "environment": "sandbox",
        "title": "OpenBuchhaltung",
        "body": "1 neuer Bankumsatz",
        "category": "bank_transactions",
        "thread_id": "company-1",
        "data": {"event": "bank_transactions"},
    }
    values.update(overrides)
    return Notification(**values)


# --- Konfiguration ------------------------------------------------------------------


def test_instance_keys_and_config_errors(tmp_path):
    digest = hash_instance_key("x")
    assert parse_instance_keys(f" a:{digest} , b:{digest.upper()} ") == {digest: "b"}
    with pytest.raises(ConfigError):
        parse_instance_keys("kaputt")
    with pytest.raises(ConfigError):
        create_app({"RELAY_INSTANCE_KEYS": ""}, sender=DryRunSender())
    with pytest.raises(ConfigError, match="RELAY_APNS_KEY_FILE"):
        create_app({"RELAY_INSTANCE_KEYS": f"a:{digest}", "RELAY_APNS_MODE": "apns"})
    with pytest.raises(ConfigError):
        create_app({"RELAY_INSTANCE_KEYS": f"a:{digest}", "RELAY_APNS_MODE": "unsinn"})

    key_file = tmp_path / "apns.p8"
    key_file.write_bytes(
        _private_key().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    assert isinstance(load_private_key(str(key_file)), ec.EllipticCurvePrivateKey)
    app = create_app(
        {
            "RELAY_INSTANCE_KEYS": f"a:{digest}",
            "RELAY_APNS_KEY_FILE": str(key_file),
            "RELAY_APNS_KEY_ID": "KEY123",
            "RELAY_APNS_TEAM_ID": "TEAM123",
        }
    )
    assert app.test_client().get("/health").get_json() == {
        "status": "ok",
        "mode": "apns",
        "instances": 1,
    }


def test_cli_creates_instance_key(capsys):
    assert relay_cli(["new-instance-key", "webbox"]) == 0
    output = capsys.readouterr().out
    key = output.split("PUSH_RELAY_TOKEN=")[1].split()[0]
    assert f"webbox:{hash_instance_key(key)}" in output
    assert relay_cli(["new-instance-key", "mit leerzeichen"]) == 2
    assert relay_cli([]) == 2


# --- Protokoll ----------------------------------------------------------------------


def test_relay_requires_instance_key():
    client = _relay().test_client()
    assert _post(client, [_item()], key="falsch").status_code == 401
    assert client.post("/v1/notifications", json={"notifications": []}).status_code == 401


def test_relay_validates_batch_and_items():
    client = _relay().test_client()
    assert _post(client, [_item(i) for i in range(101)]).status_code == 400
    assert _post(client, [{"device_token": TOKEN}]).status_code == 400
    assert client.post(
        "/v1/notifications", data="kein json", headers={"Authorization": f"Bearer {KEY}"}
    ).status_code == 400

    response = _post(
        client,
        [
            _item(1),
            _item(2, device_token="xyz"),
            _item(3, environment="staging"),
            _item(4, body=""),
            _item(5, title="x" * 101),
            _item(6, data={"blob": "x" * 2000}),
            _item(7, device_token=TOKEN.upper()),
        ],
    )
    assert response.status_code == 200
    results = {result["id"]: result for result in response.get_json()["results"]}
    assert results[1]["status"] == "sent"
    assert results[7]["status"] == "sent"
    for item_id in (2, 3, 4, 5, 6):
        assert results[item_id]["status"] == "error"
        assert results[item_id]["error"]


def test_rate_limit_per_instance():
    clock = [0.0]
    limiter = RateLimiter(2, clock=lambda: clock[0])
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    clock[0] = 60.0
    assert limiter.allow("a")

    client = _relay(RELAY_RATE_LIMIT_PER_MINUTE=2).test_client()
    results = _post(client, [_item(i) for i in range(3)]).get_json()["results"]
    statuses = [result["status"] for result in results]
    assert statuses == ["sent", "sent", "error"]


# --- APNs ---------------------------------------------------------------------------


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_provider_token_is_valid_es256_jwt():
    key = _private_key()
    token = provider_token(key, key_id="KEY123", team_id="TEAM123", issued_at=1_700_000_000)
    header, claims, signature = token.split(".")
    assert json.loads(_b64decode(header)) == {"alg": "ES256", "kid": "KEY123"}
    assert json.loads(_b64decode(claims)) == {"iss": "TEAM123", "iat": 1_700_000_000}
    raw = _b64decode(signature)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


class FakeApns:
    """httpx-MockTransport, der APNs nachbildet und Anfragen protokolliert."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, reason = self.responses.pop(0) if self.responses else (200, None)
        return httpx.Response(status, json={"reason": reason} if reason else None)


def _sender(apns: FakeApns, clock=lambda: 1_700_000_000.0) -> ApnsSender:
    client = httpx.Client(
        transport=httpx.MockTransport(apns), base_url="https://api.sandbox.push.apple.com"
    )
    return ApnsSender(
        private_key=_private_key(),
        key_id="KEY123",
        team_id="TEAM123",
        topic="de.openbuchhaltung.app",
        clients={"sandbox": client, "production": client},
        clock=clock,
    )


def test_apns_sender_builds_request():
    apns = FakeApns()
    result = _sender(apns).send(_notification())
    assert result.status == "sent"
    [request] = apns.requests
    assert request.url.path == f"/3/device/{TOKEN}"
    assert request.headers["apns-topic"] == "de.openbuchhaltung.app"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["apns-expiration"] == str(1_700_000_000 + 86400)
    assert request.headers["authorization"].startswith("bearer ")
    body = json.loads(request.content)
    assert body["aps"]["alert"] == {"title": "OpenBuchhaltung", "body": "1 neuer Bankumsatz"}
    assert body["aps"]["thread-id"] == "company-1"
    assert body["aps"]["category"] == "bank_transactions"
    assert body["aps"]["mutable-content"] == 1
    assert body["data"] == {"event": "bank_transactions"}


def test_apns_sender_maps_responses():
    assert _sender(FakeApns((410, "Unregistered"))).send(_notification()).status == "invalid_token"
    bad = _sender(FakeApns((400, "BadDeviceToken"))).send(_notification())
    assert (bad.status, bad.error) == ("error", "BadDeviceToken")
    throttled = _sender(FakeApns((429, "TooManyRequests"))).send(_notification())
    assert throttled.error == "TooManyRequests"
    too_large = _sender(FakeApns()).send(_notification(body="ü" * 2100))
    assert too_large.error == "PayloadTooLarge"


def test_apns_sender_refreshes_expired_provider_token():
    apns = FakeApns((403, "ExpiredProviderToken"), (200, None))
    clock = [1_700_000_000.0]
    sender = _sender(apns, clock=lambda: clock[0])
    assert sender.send(_notification()).status == "sent"
    first, second = (request.headers["authorization"] for request in apns.requests)
    assert first != second

    # Token wird wiederverwendet und erst nach 50 Minuten erneuert.
    sender.send(_notification())
    assert apns.requests[-1].headers["authorization"] == second
    clock[0] += 50 * 60
    sender.send(_notification())
    assert apns.requests[-1].headers["authorization"] != second


def test_apns_sender_reports_network_errors():
    def broken(request):
        raise httpx.ConnectError("down", request=request)

    client = httpx.Client(transport=httpx.MockTransport(broken), base_url="https://x")
    sender = ApnsSender(
        private_key=_private_key(),
        key_id="K",
        team_id="T",
        topic="de.openbuchhaltung.app",
        clients={"sandbox": client},
    )
    result = sender.send(_notification())
    assert result.status == "error" and "ConnectError" in result.error


# --- Ende zu Ende: Instanz → Relay → APNs --------------------------------------------


def test_instance_dispatch_through_relay_to_apns(tmp_path):
    instance = _create_app(tmp_path, PUSH_RELAY_TOKEN=KEY)
    ids = _seed(instance)
    _register(instance.test_client(), "maria")
    _register(instance.test_client(), "chef", token="c" * 64)
    with instance.app_context(), instance.extensions["db_session_factory"]() as session:
        import_bank_csv(
            session=session,
            company_id=ids["company_id"],
            bank_account_id=ids["bank_id"],
            csv_stream=StringIO("Buchungstag;Verwendungszweck;Betrag\n05.07.2026;Miete;-595,00\n"),
            changed_by="pytest",
        )

    apns = FakeApns((200, None), (410, "Unregistered"))
    relay_client = _relay(sender=_sender(apns)).test_client()

    def transport(url, token, payload, timeout):
        assert url.endswith("/v1/notifications")
        response = relay_client.post(
            "/v1/notifications", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        return response.get_json()

    with instance.extensions["db_session_factory"]() as session:
        counts = dispatch_pending(
            session,
            config=instance.config,
            transport=transport,
            now=datetime.now(timezone.utc),
        )
    assert counts["sent"] == 1 and counts["failed"] == 1
    assert [request.url.path for request in apns.requests] == [
        f"/3/device/{TOKEN_A}",
        f"/3/device/{'c' * 64}",
    ]
    devices = instance.test_client().get(
        "/api/v1/devices?include_inactive=1", headers={"Authorization": "Bearer token-chef"}
    ).get_json()["devices"]
    assert {device["token_suffix"]: device["active"] for device in devices} == {
        TOKEN_A[-8:]: True,
        "c" * 8: False,
    }
