"""Demo-Instanz: Demo-Daten, täglicher Reset und gesperrte Bereiche (DEMO_MODE)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import create_app
from app.services.bank_proposals import booking_proposals
from app.services.demo import demo_block_reason
from app.services.journal_entries import finalize_journal_entry
from domain.models import BankTransaction, Document, JournalEntry, OpenItem, User


def _create_app(tmp_path: Path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'demo.db'}",
        "DOCUMENT_UPLOAD_DIR": str(tmp_path / "uploads"),
        "DEMO_MODE": True,
    }
    config.update(overrides)
    return create_app(config)


def _reset(app):
    result = app.test_cli_runner().invoke(args=["demo-reset"])
    assert result.exit_code == 0, result.output
    return result


@pytest.mark.parametrize(
    ("path", "method", "blocked"),
    [
        ("/api/v1/users", "POST", True),
        ("/api/v1/users/me/password", "POST", True),
        ("/api/v1/users/3/llm", "POST", True),
        ("/api/v1/users", "GET", False),
        ("/users/llm", "POST", True),
        ("/auth/password", "POST", True),
        ("/api/v1/fints-connections", "POST", True),
        ("/bank/open-banking/anlegen", "POST", True),
        ("/api/v1/elster/ustva/submit", "POST", True),
        ("/api/v1/elster/ustva/preflight", "POST", False),
        ("/ustva/4/elster-test", "POST", True),
        ("/api/v1/bank-transactions/5/book", "POST", False),
        ("/api/v1/documents", "POST", False),
        ("/auth/login", "POST", False),
        ("/oauth/token", "POST", False),
    ],
)
def test_demo_block_rules(path, method, blocked):
    assert (demo_block_reason(path, method) is not None) is blocked


def test_demo_reset_seeds_public_demo_and_activity(tmp_path):
    app = _create_app(tmp_path)
    output = _reset(app).output
    assert "Demo-Datenbank zurückgesetzt." in output

    with app.extensions["db_session_factory"]() as session:
        usernames = session.execute(select(User.username)).scalars().all()
        assert usernames == ["demo"]  # keine Admins mit bekannten Passwörtern

        open_transactions = session.execute(
            select(BankTransaction).where(BankTransaction.status == "open")
        ).scalars().all()
        proposals = booking_proposals(session=session, transactions=open_transactions)
        sources = {
            tx.purpose: proposals[tx.id].source for tx in open_transactions if tx.id in proposals
        }
        assert sources["Telekom Rechnung"] == "history"
        assert sources["Abschlag Strom"] == "history"
        assert sources["Google Ads Kampagne"] == "rule"
        assert "Kontoführung" not in sources

        overdue = session.execute(
            select(func.count()).select_from(OpenItem).where(OpenItem.due_date < date.today())
        ).scalar_one()
        assert overdue == 1
        [document] = session.execute(select(Document)).scalars().all()
        assert document.journal_entry_id is None
        assert Path(document.storage_key).read_bytes().startswith(b"%PDF-")


def test_demo_reset_wipes_finalized_entries_and_uploads(tmp_path):
    app = _create_app(tmp_path)
    _reset(app)
    stray = Path(app.config["DOCUMENT_UPLOAD_DIR"]) / "fremd.pdf"
    stray.write_bytes(b"%PDF-1.4 fremd")
    with app.extensions["db_session_factory"]() as session:
        entry = session.execute(select(JournalEntry).order_by(JournalEntry.id)).scalars().first()
        # Festgeschriebene Buchungen sind per Trigger gegen Löschen geschützt.
        finalize_journal_entry(session=session, journal_entry_id=entry.id, changed_by="test")
        session.commit()
        count_before = session.execute(select(func.count()).select_from(JournalEntry)).scalar_one()

    _reset(app)
    assert not stray.exists()
    with app.extensions["db_session_factory"]() as session:
        count_after = session.execute(select(func.count()).select_from(JournalEntry)).scalar_one()
        finalized = session.execute(
            select(func.count()).select_from(JournalEntry).where(
                JournalEntry.is_finalized.is_(True)
            )
        ).scalar_one()
    assert count_after == count_before
    assert finalized == 0


def test_demo_reset_refused_in_production_without_demo_mode(tmp_path):
    app = _create_app(tmp_path, DEMO_MODE=False, TESTING=False, APP_ENV="production",
                      SECRET_KEY="x" * 40)
    result = app.test_cli_runner().invoke(args=["demo-reset"])
    assert result.exit_code != 0
    assert "DEMO_MODE" in result.output


def test_demo_mode_login_banner_and_blocked_areas(tmp_path):
    app = _create_app(tmp_path)
    _reset(app)
    client = app.test_client()

    login = client.get("/auth/login").get_data(as_text=True)
    assert 'value="demo"' in login and 'value="demo1234"' in login
    assert "Demo-Instanz" in login

    assert client.post(
        "/auth/login", data={"username": "demo", "password": "demo1234"}
    ).status_code == 302

    blocked_api = client.post("/api/v1/users/me/password", json={"new_password": "x"})
    assert blocked_api.status_code == 403
    assert blocked_api.get_json()["demo"] is True

    blocked_web = client.post("/users/llm", data={}, headers={"Referer": "/verwaltung"})
    assert blocked_web.status_code == 302
    assert blocked_web.headers["Location"].endswith("/verwaltung")
    page = client.get("/verwaltung").get_data(as_text=True)
    assert "In der Demo-Instanz gesperrt" in page

    listing = client.get(
        "/api/v1/bank-transactions",
        query_string={"company_id": 1, "status": "open", "include_suggestions": "1"},
    ).get_json()
    telekom = next(t for t in listing["transactions"] if t["purpose"] == "Telekom Rechnung")
    proposal = telekom["booking_proposal"]
    booked = client.post(
        f"/api/v1/bank-transactions/{telekom['id']}/book",
        json={"contra_account_id": proposal["contra_account_id"],
              "tax_code_id": proposal["tax_code_id"]},
    )
    assert booked.status_code == 201, booked.get_json()  # Buchen bleibt erlaubt


def test_seed_demo_in_development_includes_activity_idempotently(tmp_path):
    app = _create_app(tmp_path, DEMO_MODE=False)
    runner = app.test_cli_runner()
    assert runner.invoke(args=["seed-demo"]).exit_code == 0
    second = runner.invoke(args=["seed-demo"])
    assert second.exit_code == 0
    assert "Demo-Alltag übersprungen" in second.output
    with app.extensions["db_session_factory"]() as session:
        usernames = set(session.execute(select(User.username)).scalars())
        assert {"admin", "buchhalter"} <= usernames
        assert session.execute(select(func.count()).select_from(BankTransaction)).scalar_one() == 8


def _mcp_tool(client, name: str, arguments: dict) -> dict:
    response = client.post(
        "/api/v1/mcp/call",
        json={"method": "tools/call", "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()["result"]


def test_demo_mode_blocks_locked_areas_via_mcp(tmp_path):
    """Die MCP-Tools laufen über die eigene REST-API: dieselben Sperren greifen."""
    app = _create_app(tmp_path)
    _reset(app)
    client = app.test_client()
    assert client.post(
        "/auth/login", data={"username": "demo", "password": "demo1234"}
    ).status_code == 302
    with app.extensions["db_session_factory"]() as session:
        demo_user_id = session.execute(select(User.id)).scalar_one()

    blocked_calls = (
        ("create_user", {"username": "mallory", "password": "geheim12345"}),
        ("rotate_user_api_token", {"user_id": demo_user_id}),
        ("change_own_password", {"current_password": "demo1234", "new_password": "x" * 12}),
        ("set_user_llm_settings", {"user_id": demo_user_id, "api_key": "sk-test"}),
        ("create_tenant_with_company", {"tenant_name": "T", "company_name": "C"}),
        ("create_fints_connection", {"company_id": 1, "bank_account_id": 1, "name": "Bank",
                                     "blz": "12345678", "login": "demo"}),
        ("start_open_banking_authorization", {"connection_id": 1}),
        ("submit_vat_return_elster", {"vat_return_id": 1}),
    )
    for name, arguments in blocked_calls:
        result = _mcp_tool(client, name, arguments)
        assert result["isError"] is True, name
        assert "In der Demo-Instanz gesperrt" in result["content"][0]["text"], name

    accounts = _mcp_tool(client, "list_accounts", {"company_id": 1})
    assert accounts["isError"] is False  # Lesen und Buchen bleiben erlaubt
