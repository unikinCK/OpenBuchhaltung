from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services import open_banking
from app.services.bank_statement import (
    BankStatementRow,
    BankStatementRowError,
    row_from_open_banking_transaction,
)
from app.services.open_banking import (
    OpenBankingConfig,
    OpenBankingError,
    OpenBankingHttpError,
    build_jwt,
    complete_open_banking_authorization,
    connection_status,
    create_open_banking_connection,
    default_sync_range,
    open_banking_config,
    set_open_banking_connection_active,
    start_open_banking_authorization,
    sync_open_banking_transactions,
)
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    OpenBankingConnection,
    Tenant,
    User,
)

SECRET = "test-secret"
IBAN = "DE89370400440532013000"
OTHER_IBAN = "DE02120300000000202051"


def _private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


PRIVATE_KEY_PEM = _private_key_pem()
CONFIG = OpenBankingConfig(
    app_id="app-123",
    private_key_pem=PRIVATE_KEY_PEM,
    redirect_url="https://buchhaltung.example.de/bank/open-banking/callback",
)


def _tx(ref, amount, indicator, booking_date="2026-10-01", status="BOOK", **extra):
    return {
        "entry_reference": ref,
        "transaction_amount": {"currency": "EUR", "amount": amount},
        "credit_debit_indicator": indicator,
        "status": status,
        "booking_date": booking_date,
        "remittance_information": [f"Zahlung {ref}"],
        "creditor": {"name": "Lieferant GmbH"},
        "debtor": {"name": "Kunde AG"},
        **extra,
    }


class FakeEnableBanking:
    """Antwortet wie die Enable-Banking-API und protokolliert die Aufrufe."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.accounts = [{"uid": "acc-1", "account_id": {"iban": IBAN}, "currency": "EUR"}]
        self.pages = [
            {
                "transactions": [
                    _tx("r1", "100.00", "CRDT"),
                    _tx("r2", "25.50", "DBIT"),
                    _tx("r3", "9.99", "DBIT", status="PDNG"),
                ],
                "continuation_key": "page-2",
            },
            {"transactions": [_tx("r4", "12.00", "DBIT", booking_date="2026-10-02")]},
        ]
        self.transactions_error: OpenBankingError | None = None

    def get(self, path, params=None):
        self.calls.append(("GET", path, dict(params or {})))
        if path == "/aspsps":
            return {
                "aspsps": [
                    {
                        "name": "Finom",
                        "country": "DE",
                        "bic": "FNOMDEB2",
                        "psu_types": ["business"],
                        "maximum_consent_validity": 180 * 86400,
                    },
                    {"name": "Andere Bank", "country": "DE", "psu_types": ["personal"]},
                ]
            }
        if path.endswith("/transactions"):
            if self.transactions_error is not None:
                raise self.transactions_error
            key = (params or {}).get("continuation_key")
            return self.pages[1] if key == "page-2" else self.pages[0]
        raise AssertionError(f"unerwarteter GET {path}")

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        if path == "/auth":
            return {"url": "https://bank.example/consent?x=1", "authorization_id": "auth-1"}
        if path == "/sessions":
            return {
                "session_id": "sess-geheim",
                "accounts": self.accounts,
                "access": {"valid_until": "2027-04-01T00:00:00+00:00"},
            }
        raise AssertionError(f"unerwarteter POST {path}")

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return {"message": "OK"}


@pytest.fixture()
def fake(monkeypatch) -> FakeEnableBanking:
    fake = FakeEnableBanking()
    monkeypatch.setattr(open_banking, "_build_client", lambda config: fake)
    return fake


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account]:
    tenant = Tenant(name="OB Tenant")
    company = Company(tenant=tenant, name="OB GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    bank = Account(
        tenant_id=tenant.id, company_id=company.id, code="1200", name="Bank", account_type="asset"
    )
    session.add(bank)
    session.commit()
    return company, bank


def _create(session, company, bank, **kwargs) -> OpenBankingConnection:
    return create_open_banking_connection(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        name=kwargs.pop("name", "Finom Geschäftskonto"),
        aspsp_name=kwargs.pop("aspsp_name", "Finom"),
        aspsp_country="DE",
        changed_by="tester",
        **kwargs,
    )


def _authorize(session, connection) -> OpenBankingConnection:
    start_open_banking_authorization(
        session=session, connection_id=connection.id, config=CONFIG, changed_by="tester"
    )
    state = session.get(OpenBankingConnection, connection.id).auth_state
    return complete_open_banking_authorization(
        session=session,
        state=state,
        code="code-1",
        config=CONFIG,
        secret_key=SECRET,
        changed_by="tester",
    )


# ---------------------------------------------------------------------------
# JWT und Mapping


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_build_jwt_is_valid_rs256_token():
    token = build_jwt(CONFIG, now=1_700_000_000)
    header_b64, payload_b64, signature_b64 = token.split(".")
    assert json.loads(_b64decode(header_b64)) == {"typ": "JWT", "alg": "RS256", "kid": "app-123"}
    payload = json.loads(_b64decode(payload_b64))
    assert payload["iss"] == "enablebanking.com"
    assert payload["aud"] == "api.enablebanking.com"
    assert payload["exp"] - payload["iat"] <= 86400

    public_key = serialization.load_pem_private_key(PRIVATE_KEY_PEM, password=None).public_key()
    public_key.verify(
        _b64decode(signature_b64),
        f"{header_b64}.{payload_b64}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_build_jwt_rejects_invalid_key():
    config = OpenBankingConfig(
        app_id="x", private_key_pem=b"kein schluessel", redirect_url="https://a"
    )
    with pytest.raises(OpenBankingError, match="PEM"):
        build_jwt(config)


def test_row_mapping_signs_amount_and_picks_counterparty():
    debit = row_from_open_banking_transaction(1, _tx("ref-1", "25.5", "DBIT"))
    assert isinstance(debit, BankStatementRow)
    assert debit.amount == Decimal("-25.50")
    assert debit.counterparty == "Lieferant GmbH"
    assert debit.purpose == "Zahlung ref-1"
    assert debit.bank_reference == "ref-1"
    assert debit.booking_date == date(2026, 10, 1)

    credit = row_from_open_banking_transaction(2, _tx("ref-2", "-100.00", "CRDT"))
    assert credit.amount == Decimal("100.00")
    assert credit.counterparty == "Kunde AG"


def test_row_mapping_fallbacks_and_errors():
    row = row_from_open_banking_transaction(
        1,
        {
            "transaction_amount": {"currency": "eur", "amount": "5"},
            "credit_debit_indicator": "DBIT",
            "value_date": "2026-09-30",
            "bank_transaction_code": {"description": "Kontoführung"},
            "transaction_id": "tx-9",
        },
    )
    assert row.purpose == "Kontoführung"
    assert row.booking_date == date(2026, 9, 30)
    assert row.bank_reference == "tx-9"
    assert row.currency_code == "EUR"

    missing_amount = row_from_open_banking_transaction(2, {"booking_date": "2026-10-01"})
    assert isinstance(missing_amount, BankStatementRowError)
    missing_date = row_from_open_banking_transaction(
        3, {"transaction_amount": {"amount": "1"}, "credit_debit_indicator": "CRDT"}
    )
    assert isinstance(missing_date, BankStatementRowError)


# ---------------------------------------------------------------------------
# Konfiguration


def test_config_requires_app_id_key_and_https_redirect(tmp_path):
    with pytest.raises(OpenBankingError, match="ENABLE_BANKING_APP_ID"):
        open_banking_config({})
    with pytest.raises(OpenBankingError, match="nicht lesbar"):
        open_banking_config(
            {"ENABLE_BANKING_APP_ID": "a", "ENABLE_BANKING_KEY_FILE": str(tmp_path / "fehlt.pem")}
        )
    key_file = tmp_path / "key.pem"
    key_file.write_bytes(PRIVATE_KEY_PEM)
    base = {"ENABLE_BANKING_APP_ID": "a", "ENABLE_BANKING_KEY_FILE": str(key_file)}
    with pytest.raises(OpenBankingError, match="https"):
        open_banking_config(base, fallback_redirect_url="http://localhost/cb")
    with pytest.raises(OpenBankingError, match="API_URL"):
        open_banking_config(
            {**base, "ENABLE_BANKING_API_URL": "http://evil.example"},
            fallback_redirect_url="https://x/cb",
        )
    config = open_banking_config(
        {**base, "ENABLE_BANKING_REDIRECT_URL": "https://konfiguriert/cb"},
        fallback_redirect_url="https://fallback/cb",
    )
    assert config.redirect_url == "https://konfiguriert/cb"
    assert config.api_url == "https://api.enablebanking.com"


# ---------------------------------------------------------------------------
# Service


def test_create_validates_input(session):
    company, bank = _seed_company(session)
    with pytest.raises(OpenBankingError, match="Land"):
        create_open_banking_connection(
            session=session,
            company_id=company.id,
            bank_account_id=bank.id,
            name="X",
            aspsp_name="Finom",
            aspsp_country="Deutschland",
            changed_by="t",
        )
    with pytest.raises(OpenBankingError, match="Kontotyp"):
        _create(session, company, bank, psu_type="firma")
    with pytest.raises(OpenBankingError, match="IBAN"):
        _create(session, company, bank, iban="123")
    connection = _create(session, company, bank, iban="de89 3704 0044 0532 0130 00")
    assert connection.iban == IBAN
    assert connection_status(connection) == "authorization_required"
    with pytest.raises(OpenBankingError, match="existiert bereits"):
        _create(session, company, bank)


def test_full_lifecycle(session, fake):
    company, bank = _seed_company(session)
    connection = _create(session, company, bank)

    start = start_open_banking_authorization(
        session=session,
        connection_id=connection.id,
        config=CONFIG,
        valid_days=365,
        changed_by="tester",
    )
    assert start.url == "https://bank.example/consent?x=1"
    auth_body = next(body for method, path, body in fake.calls if path == "/auth")
    assert auth_body["aspsp"] == {"name": "Finom", "country": "DE"}
    assert auth_body["psu_type"] == "business"
    assert auth_body["redirect_url"] == CONFIG.redirect_url
    # Gekappt auf die maximale Gültigkeit der Bank (180 Tage).
    assert (start.valid_until - datetime.now(timezone.utc)).days in (179, 180)
    state = session.get(OpenBankingConnection, connection.id).auth_state
    assert state and auth_body["state"] == state

    connection = complete_open_banking_authorization(
        session=session,
        state=state,
        code="code-1",
        config=CONFIG,
        secret_key=SECRET,
        changed_by="tester",
    )
    assert connection.auth_state is None
    assert connection.account_uid == "acc-1"
    assert connection.iban == IBAN
    assert "sess-geheim" not in (connection.session_id_encrypted or "")
    assert connection_status(connection) == "authorized"

    report = sync_open_banking_transactions(
        session=session,
        connection_id=connection.id,
        config=CONFIG,
        secret_key=SECRET,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 10, 5),
        changed_by="tester",
    )
    # Vorgemerkter Umsatz (PDNG) bleibt außen vor; zwei Seiten werden gelesen.
    assert report.imported_rows == 3
    tx_calls = [params for method, path, params in fake.calls if path.endswith("/transactions")]
    assert tx_calls[0] == {"date_from": "2026-09-01", "date_to": "2026-10-05"}
    assert tx_calls[1]["continuation_key"] == "page-2"
    amounts = sorted(
        session.execute(select(BankTransaction.amount)).scalars(), key=lambda value: value
    )
    assert amounts == [Decimal("-25.50"), Decimal("-12.00"), Decimal("100.00")]
    assert session.get(OpenBankingConnection, connection.id).last_synced_to == date(2026, 10, 5)

    again = sync_open_banking_transactions(
        session=session,
        connection_id=connection.id,
        config=CONFIG,
        secret_key=SECRET,
        from_date=date(2026, 9, 1),
        to_date=date(2026, 10, 5),
        changed_by="tester",
    )
    assert again.imported_rows == 0
    assert again.duplicate_rows == 3

    deactivated = set_open_banking_connection_active(
        session=session,
        connection_id=connection.id,
        is_active=False,
        changed_by="tester",
        config=CONFIG,
        secret_key=SECRET,
    )
    assert ("DELETE", "/sessions/sess-geheim", None) in fake.calls
    assert deactivated.session_id_encrypted is None
    assert connection_status(deactivated) == "inactive"

    actions = set(
        session.execute(
            select(AuditLog.action).where(AuditLog.entity_type == "open_banking_connection")
        ).scalars()
    )
    assert {"created", "authorization_started", "authorized", "deactivated"} <= actions
    # Die Sitzungs-ID landet nie im Audit-Log.
    payloads = " ".join(
        json.dumps(item) for item in session.execute(select(AuditLog.payload)).scalars()
    )
    assert "sess-geheim" not in payloads


def test_start_rejects_unknown_bank(session, fake):
    company, bank = _seed_company(session)
    connection = _create(session, company, bank, aspsp_name="Gibt Es Nicht")
    with pytest.raises(OpenBankingError, match="nicht an"):
        start_open_banking_authorization(
            session=session, connection_id=connection.id, config=CONFIG, changed_by="t"
        )


def test_complete_rejects_unknown_and_expired_state(session, fake):
    company, bank = _seed_company(session)
    connection = _create(session, company, bank)
    with pytest.raises(OpenBankingError, match="nicht gefunden"):
        complete_open_banking_authorization(
            session=session,
            state="falsch",
            code="c",
            config=CONFIG,
            secret_key=SECRET,
            changed_by="t",
        )

    start_open_banking_authorization(
        session=session, connection_id=connection.id, config=CONFIG, changed_by="t"
    )
    stored = session.get(OpenBankingConnection, connection.id)
    state = stored.auth_state
    stored.auth_started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    session.commit()
    with pytest.raises(OpenBankingError, match="abgelaufen"):
        complete_open_banking_authorization(
            session=session, state=state, code="c", config=CONFIG, secret_key=SECRET, changed_by="t"
        )
    assert session.get(OpenBankingConnection, connection.id).auth_state is None


def test_complete_selects_account_by_iban_and_rejects_ambiguity(session, fake):
    company, bank = _seed_company(session)
    fake.accounts = [
        {"uid": "acc-1", "account_id": {"iban": IBAN}},
        {"uid": "acc-2", "account_id": {"iban": OTHER_IBAN}},
    ]
    ambiguous = _create(session, company, bank, name="Ohne IBAN")
    with pytest.raises(OpenBankingError, match="mehrere Konten"):
        _authorize(session, ambiguous)
    # Die unbrauchbare Sitzung wird sofort widerrufen.
    assert ("DELETE", "/sessions/sess-geheim", None) in fake.calls
    assert session.get(OpenBankingConnection, ambiguous.id).session_id_encrypted is None

    with_iban = _create(session, company, bank, name="Mit IBAN", iban=OTHER_IBAN)
    assert _authorize(session, with_iban).account_uid == "acc-2"


def test_sync_requires_authorization_and_valid_consent(session, fake):
    company, bank = _seed_company(session)
    connection = _create(session, company, bank)
    with pytest.raises(OpenBankingError, match="nicht autorisiert"):
        sync_open_banking_transactions(
            session=session,
            connection_id=connection.id,
            config=CONFIG,
            secret_key=SECRET,
            changed_by="t",
        )

    connection = _authorize(session, connection)
    with pytest.raises(OpenBankingError, match="nicht autorisiert"):
        # Anderer SECRET_KEY: Sitzung nicht entschlüsselbar → neu autorisieren.
        sync_open_banking_transactions(
            session=session,
            connection_id=connection.id,
            config=CONFIG,
            secret_key="anders",
            changed_by="t",
        )

    fake.transactions_error = OpenBankingHttpError("Enable Banking meldet HTTP 401.", 401)
    with pytest.raises(OpenBankingError, match="erneuern"):
        sync_open_banking_transactions(
            session=session,
            connection_id=connection.id,
            config=CONFIG,
            secret_key=SECRET,
            changed_by="t",
        )

    connection.consent_valid_until = datetime.now(timezone.utc) - timedelta(days=1)
    session.commit()
    assert connection_status(connection) == "expired"
    with pytest.raises(OpenBankingError, match="abgelaufen"):
        sync_open_banking_transactions(
            session=session,
            connection_id=connection.id,
            config=CONFIG,
            secret_key=SECRET,
            changed_by="t",
        )


def test_default_sync_range(session):
    company, bank = _seed_company(session)
    connection = _create(session, company, bank)
    today = date(2026, 10, 11)
    assert default_sync_range(session, connection, today=today) == (date(2026, 7, 13), today)

    session.add(
        BankTransaction(
            tenant_id=company.tenant_id,
            company_id=company.id,
            bank_account_id=bank.id,
            booking_date=date(2026, 10, 3),
            amount=Decimal("1.00"),
            currency_code="EUR",
            purpose="CSV-Import",
            dedup_hash="x",
        )
    )
    session.commit()
    assert default_sync_range(session, connection, today=today) == (date(2026, 10, 4), today)

    connection.last_synced_to = date(2026, 10, 9)
    session.commit()
    assert default_sync_range(session, connection, today=today) == (date(2026, 10, 2), today)


# ---------------------------------------------------------------------------
# API und Web


@pytest.fixture()
def ob_app(tmp_path):
    key_file = tmp_path / "enablebanking.pem"
    key_file.write_bytes(PRIVATE_KEY_PEM)
    app = create_app(
        {
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'ob.sqlite'}",
            "TESTING": True,
            "SECRET_KEY": SECRET,
            "API_REQUIRE_AUTH": False,
            "CSRF_PROTECT": False,
            "ENABLE_BANKING_APP_ID": "app-123",
            "ENABLE_BANKING_KEY_FILE": str(key_file),
            "ENABLE_BANKING_REDIRECT_URL": CONFIG.redirect_url,
        }
    )
    with app.extensions["db_session_factory"]() as session:
        company, bank = _seed_company(session)
        session.add(
            User(
                tenant_id=company.tenant_id,
                username="ob-admin",
                password_hash=hash_password("secret"),
                role="Admin",
            )
        )
        session.commit()
        ids = (company.id, bank.id)
    return app, ids


def test_api_lifecycle(ob_app, fake):
    app, (company_id, bank_account_id) = ob_app
    with app.test_client() as client:
        institutes = client.get("/api/v1/open-banking/institutes", query_string={"q": "fin"})
        assert institutes.status_code == 200, institutes.get_json()
        assert [item["name"] for item in institutes.get_json()["institutes"]] == ["Finom"]
        assert institutes.get_json()["institutes"][0]["maximum_consent_validity_days"] == 180

        created = client.post(
            "/api/v1/open-banking-connections",
            json={
                "company_id": company_id,
                "bank_account_id": bank_account_id,
                "name": "Finom",
                "aspsp_name": "Finom",
                "aspsp_country": "DE",
            },
        )
        assert created.status_code == 201, created.get_json()
        connection_id = created.get_json()["id"]
        assert created.get_json()["status"] == "authorization_required"

        authorize = client.post(
            f"/api/v1/open-banking-connections/{connection_id}/authorize", json={}
        )
        assert authorize.status_code == 200, authorize.get_json()
        assert authorize.get_json()["authorization_url"].startswith("https://bank.example/")
        state = next(body for _, path, body in fake.calls if path == "/auth")["state"]

        completed = client.post(
            "/api/v1/open-banking/complete",
            json={"redirect_url": f"{CONFIG.redirect_url}?code=abc&state={state}"},
        )
        assert completed.status_code == 200, completed.get_json()
        assert completed.get_json()["status"] == "authorized"
        assert "session" not in json.dumps(completed.get_json())

        sync = client.post(
            f"/api/v1/open-banking-connections/{connection_id}/sync",
            json={"from_date": "2026-09-01", "to_date": "2026-10-05"},
        )
        assert sync.status_code == 200, sync.get_json()
        assert sync.get_json()["report"]["imported_rows"] == 3

        bad_date = client.post(
            f"/api/v1/open-banking-connections/{connection_id}/sync", json={"from_date": "gestern"}
        )
        assert bad_date.status_code == 422

        listing = client.get(
            "/api/v1/open-banking-connections", query_string={"company_id": company_id}
        )
        assert listing.get_json()["connections"][0]["last_synced_to"] == "2026-10-05"

        deactivated = client.post(
            f"/api/v1/open-banking-connections/{connection_id}/active", json={"is_active": False}
        )
        assert deactivated.status_code == 200
        assert deactivated.get_json()["status"] == "inactive"


def test_api_complete_reports_bank_error_and_unknown_state(ob_app, fake):
    app, _ = ob_app
    with app.test_client() as client:
        error = client.post(
            "/api/v1/open-banking/complete",
            json={"redirect_url": "https://x/cb?error=access_denied&state=s"},
        )
        assert error.status_code == 422
        unknown = client.post("/api/v1/open-banking/complete", json={"code": "c", "state": "s"})
        assert unknown.status_code == 404
        missing = client.post("/api/v1/open-banking/complete", json={})
        assert missing.status_code == 400


def test_web_authorize_redirects_to_bank_and_callback_completes(ob_app, fake):
    app, (company_id, bank_account_id) = ob_app
    with app.test_client() as client:
        client.post("/auth/login", data={"username": "ob-admin", "password": "secret"})
        created = client.post(
            "/bank/open-banking/anlegen",
            data={
                "company_id": company_id,
                "bank_account_id": bank_account_id,
                "name": "Finom",
                "aspsp_name": "Finom",
                "aspsp_country": "DE",
                "psu_type": "business",
            },
        )
        assert created.status_code == 302
        with app.extensions["db_session_factory"]() as session:
            connection_id = session.execute(select(OpenBankingConnection.id)).scalar_one()

        page = client.get(f"/bank?company_id={company_id}")
        assert "Bank-Zustimmung erteilen" in page.get_data(as_text=True)

        redirect = client.post(f"/bank/open-banking/{connection_id}/autorisieren")
        assert redirect.status_code == 302
        assert redirect.headers["Location"] == "https://bank.example/consent?x=1"
        state = next(body for _, path, body in fake.calls if path == "/auth")["state"]

        callback = client.get(f"/bank/open-banking/callback?code=abc&state={state}")
        assert callback.status_code == 302
        with app.extensions["db_session_factory"]() as session:
            connection = session.get(OpenBankingConnection, connection_id)
            assert connection_status(connection) == "authorized"

        synced = client.post(
            f"/bank/open-banking/{connection_id}/abrufen",
            data={"from_date": "2026-09-01", "to_date": "2026-10-05"},
            follow_redirects=True,
        )
        assert "Open-Banking-Abruf: 3 neu" in synced.get_data(as_text=True)


def test_web_callback_rejects_foreign_tenant(ob_app, fake):
    app, (company_id, bank_account_id) = ob_app
    with app.extensions["db_session_factory"]() as session:
        other_tenant = Tenant(name="Fremd")
        other_company = Company(tenant=other_tenant, name="Fremd GmbH", currency_code="EUR")
        session.add_all([other_tenant, other_company])
        session.flush()
        other_bank = Account(
            tenant_id=other_tenant.id,
            company_id=other_company.id,
            code="1200",
            name="Bank",
            account_type="asset",
        )
        session.add(other_bank)
        session.flush()
        foreign = OpenBankingConnection(
            tenant_id=other_tenant.id,
            company_id=other_company.id,
            bank_account_id=other_bank.id,
            name="Fremd",
            aspsp_name="Finom",
            aspsp_country="DE",
            psu_type="business",
            auth_state="fremder-state",
            auth_started_at=datetime.now(timezone.utc),
        )
        session.add(foreign)
        session.commit()
        foreign_id = foreign.id

    with app.test_client() as client:
        client.post("/auth/login", data={"username": "ob-admin", "password": "secret"})
        assert (
            client.get("/bank/open-banking/callback?code=c&state=fremder-state").status_code == 404
        )
        assert client.post(f"/bank/open-banking/{foreign_id}/abrufen").status_code == 404
        assert client.post(f"/bank/open-banking/{foreign_id}/autorisieren").status_code == 404
        assert client.post(f"/bank/open-banking/{foreign_id}/deaktivieren").status_code == 404

    with app.extensions["db_session_factory"]() as session:
        foreign = session.get(OpenBankingConnection, foreign_id)
        assert foreign.auth_state == "fremder-state"
        assert foreign.is_active is True
    assert not any(path == "/sessions" for _, path, _ in fake.calls)
