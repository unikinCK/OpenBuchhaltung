"""Tests für die UStVA-Kennziffern-Berechnung und Snapshots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.elster import (
    ElsterError,
    EricElsterTransport,
    MockElsterTransport,
    elster_readiness,
    elster_submission_summary,
    list_elster_submissions,
    retry_elster_submission,
    submit_vat_return,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
    reverse_journal_entry,
)
from app.services.vat_returns import (
    VatReturnError,
    compute_vat_return,
    list_vat_returns,
    period_bounds,
    save_vat_return,
)
from domain.models import Account, AuditLog, Base, Company, TaxCode, Tenant, User, VatReturn


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed(session: Session) -> Company:
    tenant = Tenant(name="UStVA Tenant")
    company = Company(tenant=tenant, name="UStVA GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    accounts = {
        "1200": ("Bank", "asset"),
        "1400": ("Forderungen", "asset"),
        "1576": ("Abziehbare Vorsteuer 19 %", "asset"),
        "1776": ("Umsatzsteuer 19 %", "liability"),
        "1771": ("Umsatzsteuer 7 %", "liability"),
        "1600": ("Verbindlichkeiten", "liability"),
        "4200": ("Miete", "expense"),
        "8400": ("Erlöse 19 %", "revenue"),
        "8300": ("Erlöse 7 %", "revenue"),
        "8100": ("Steuerfreie Erlöse", "revenue"),
    }
    for code, (name, account_type) in accounts.items():
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
            )
        )
    session.flush()

    def account_id(code: str) -> int:
        return session.scalar(
            select(Account.id).where(Account.company_id == company.id, Account.code == code)
        )

    session.add_all(
        [
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="USt19",
                rate=Decimal("19.00"),
                vat_account_id=account_id("1776"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="USt7",
                rate=Decimal("7.00"),
                vat_account_id=account_id("1771"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="VSt19",
                rate=Decimal("19.00"),
                vat_account_id=account_id("1576"),
            ),
            TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code="frei",
                rate=Decimal("0.00"),
            ),
        ]
    )
    session.commit()
    return company


def _create_test_app(tmp_path: Path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_elster.db'}",
        }
    )


def _account_id(session: Session, company: Company, code: str) -> int:
    return session.scalar(
        select(Account.id).where(Account.company_id == company.id, Account.code == code)
    )


def _tax_code_id(session: Session, company: Company, code: str) -> int:
    return session.scalar(
        select(TaxCode.id).where(TaxCode.company_id == company.id, TaxCode.code == code)
    )


def _book(session: Session, company: Company, *, entry_date, description, lines) -> None:
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description=description,
            status="posted",
            changed_by="pytest",
            lines=lines,
        ),
    )


def _seed_bookings(session: Session, company: Company) -> None:
    """Mai 2026: 1.000,50 € netto zu 19 %, 200 € netto zu 7 %, 100 € steuerfrei,
    Eingangsrechnung 500 € netto mit 19 % Vorsteuer."""
    _book(
        session,
        company,
        entry_date=date(2026, 5, 5),
        description="Ausgangsrechnung 19 %",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("1190.60"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8400"),
                credit_amount=Decimal("1000.50"),
                tax_code_id=_tax_code_id(session, company, "USt19"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 12),
        description="Ausgangsrechnung 7 %",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("214.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8300"),
                credit_amount=Decimal("200.00"),
                tax_code_id=_tax_code_id(session, company, "USt7"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 20),
        description="Steuerfreier Umsatz",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1200"),
                debit_amount=Decimal("100.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8100"),
                credit_amount=Decimal("100.00"),
                tax_code_id=_tax_code_id(session, company, "frei"),
            ),
        ],
    )
    _book(
        session,
        company,
        entry_date=date(2026, 5, 25),
        description="Eingangsrechnung Miete",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "4200"),
                debit_amount=Decimal("500.00"),
                tax_code_id=_tax_code_id(session, company, "VSt19"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "1600"),
                credit_amount=Decimal("595.00"),
            ),
        ],
    )
    # Juni-Buchung: darf im Mai nicht auftauchen.
    _book(
        session,
        company,
        entry_date=date(2026, 6, 2),
        description="Ausgangsrechnung Juni",
        lines=[
            JournalLineInput(
                account_id=_account_id(session, company, "1400"),
                debit_amount=Decimal("119.00"),
            ),
            JournalLineInput(
                account_id=_account_id(session, company, "8400"),
                credit_amount=Decimal("100.00"),
                tax_code_id=_tax_code_id(session, company, "USt19"),
            ),
        ],
    )


def _amounts_by_kz(rows) -> dict[str, Decimal]:
    return {row.kennziffer: row.amount for row in rows}


def test_period_bounds() -> None:
    assert period_bounds("2026-05") == (date(2026, 5, 1), date(2026, 5, 31), "2026-05")
    assert period_bounds("2026-q2") == (date(2026, 4, 1), date(2026, 6, 30), "2026-Q2")
    assert period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31), "2026-12")
    assert period_bounds("2026-h1") == (date(2026, 1, 1), date(2026, 6, 30), "2026-H1")
    assert period_bounds("2026-H2") == (date(2026, 7, 1), date(2026, 12, 31), "2026-H2")
    assert period_bounds("2026") == (date(2026, 1, 1), date(2026, 12, 31), "2026")
    assert period_bounds(" 2026 ") == (date(2026, 1, 1), date(2026, 12, 31), "2026")
    with pytest.raises(VatReturnError):
        period_bounds("2026-13")
    with pytest.raises(VatReturnError):
        period_bounds("2026-Q5")
    with pytest.raises(VatReturnError):
        period_bounds("2026-H3")
    with pytest.raises(VatReturnError):
        period_bounds("2026-H0")
    with pytest.raises(VatReturnError):
        period_bounds("Mai 2026")


def test_compute_vat_return_monthly(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    rows = compute_vat_return(
        session=session,
        company_id=company.id,
        date_from=date(2026, 5, 1),
        date_to=date(2026, 5, 31),
    )
    amounts = _amounts_by_kz(rows)

    # Bemessungsgrundlagen in vollen Euro abgerundet (1000,50 -> 1000).
    assert amounts["81"] == Decimal("1000")
    assert amounts["86"] == Decimal("200")
    assert amounts["48"] == Decimal("100")
    # Gebuchte Steuer: 19 % von 1000,50 = 190,10 (halb-auf) + 7 % von 200 = 14,00.
    assert amounts["USt"] == Decimal("204.10")
    assert amounts["66"] == Decimal("95.00")
    assert amounts["83"] == Decimal("109.10")


def test_compute_vat_return_quarter_includes_june(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    date_from, date_to, label = period_bounds("2026-Q2")
    amounts = _amounts_by_kz(
        compute_vat_return(
            session=session, company_id=company.id, date_from=date_from, date_to=date_to
        )
    )
    assert label == "2026-Q2"
    assert amounts["81"] == Decimal("1100")  # 1000,50 + 100 -> abgerundet
    assert amounts["83"] == Decimal("128.10")


def test_storno_neutralizes_vat_return(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    before = _amounts_by_kz(
        compute_vat_return(
            session=session,
            company_id=company.id,
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
        )
    )
    assert before["81"] == Decimal("1000")

    # Die 19 %-Ausgangsrechnung im Mai stornieren (Storno im selben Zeitraum).
    from domain.models import JournalEntry

    entry_id = session.scalar(
        select(JournalEntry.id).where(
            JournalEntry.company_id == company.id,
            JournalEntry.description == "Ausgangsrechnung 19 %",
        )
    )
    reverse_journal_entry(
        session=session,
        journal_entry_id=entry_id,
        reversal_date=date(2026, 5, 28),
        changed_by="pytest",
    )

    after = _amounts_by_kz(
        compute_vat_return(
            session=session,
            company_id=company.id,
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 31),
        )
    )
    assert after["81"] == Decimal("0")
    assert after["USt"] == Decimal("14.00")
    assert after["83"] == Decimal("-81.00")


def test_save_vat_return_snapshot_and_duplicate(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )
    assert vat_return.period_label == "2026-05"
    assert vat_return.status == "erstellt"
    amounts = {row["kennziffer"]: row["amount"] for row in vat_return.kennzahlen}
    assert amounts["83"] == "109.10"

    audit = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "vat_return",
            AuditLog.entity_id == str(vat_return.id),
            AuditLog.action == "created",
        )
    ).scalar_one()
    assert audit.payload["period_label"] == "2026-05"

    with pytest.raises(VatReturnError, match="bereits eine UStVA"):
        save_vat_return(
            session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
        )

    items = list_vat_returns(session=session, company_id=company.id)
    assert [item.period_label for item in items] == ["2026-05"]


def test_save_vat_return_normalizes_label(session: Session) -> None:
    company = _seed(session)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-q2", changed_by="pytest"
    )
    assert vat_return.period_label == "2026-Q2"


def test_compute_vat_return_half_year_and_year(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    # H1 (Jan–Jun) enthält Mai- und Juni-Buchungen: 1000,50 + 100 = 1100 (abgerundet).
    date_from, date_to, label = period_bounds("2026-H1")
    amounts = _amounts_by_kz(
        compute_vat_return(
            session=session, company_id=company.id, date_from=date_from, date_to=date_to
        )
    )
    assert label == "2026-H1"
    assert amounts["81"] == Decimal("1100")
    assert amounts["83"] == Decimal("128.10")

    # H2 (Jul–Dez) ist leer.
    date_from, date_to, _ = period_bounds("2026-H2")
    amounts_h2 = _amounts_by_kz(
        compute_vat_return(
            session=session, company_id=company.id, date_from=date_from, date_to=date_to
        )
    )
    assert amounts_h2["81"] == Decimal("0")
    assert amounts_h2["83"] == Decimal("0.00")

    # Jahr = H1 + H2.
    date_from, date_to, label = period_bounds("2026")
    amounts_year = _amounts_by_kz(
        compute_vat_return(
            session=session, company_id=company.id, date_from=date_from, date_to=date_to
        )
    )
    assert label == "2026"
    assert amounts_year["81"] == amounts["81"]
    assert amounts_year["83"] == amounts["83"]


def test_save_vat_return_half_year_and_year_snapshots(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)

    half = save_vat_return(
        session=session, company_id=company.id, period_label="2026-h1", changed_by="pytest"
    )
    assert half.period_label == "2026-H1"
    assert (half.date_from, half.date_to) == (date(2026, 1, 1), date(2026, 6, 30))

    year = save_vat_return(
        session=session, company_id=company.id, period_label="2026", changed_by="pytest"
    )
    assert year.period_label == "2026"
    assert (year.date_from, year.date_to) == (date(2026, 1, 1), date(2026, 12, 31))

    # Halbjahr und Jahr sind eigenständige Zeiträume — kein Unique-Konflikt,
    # aber dasselbe Label erneut festhalten ist verboten.
    with pytest.raises(VatReturnError, match="bereits eine UStVA"):
        save_vat_return(
            session=session, company_id=company.id, period_label="2026-H1", changed_by="pytest"
        )


def test_elster_mock_submission_updates_vat_return_and_audit(session: Session) -> None:
    company = _seed(session)
    _seed_bookings(session, company)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )

    submission = submit_vat_return(
        session=session,
        vat_return_id=vat_return.id,
        environment="test",
        transport="mock",
        changed_by="pytest",
    )
    assert submission.status == "transmitted"
    assert submission.transfer_ticket.startswith("MOCK-USTVA-2026-05-")
    assert len(submission.payload_hash) == 64
    assert "<Period>2026-05</Period>" in submission.payload_xml
    assert session.get(VatReturn, vat_return.id).status == "uebermittelt"

    submissions = list_elster_submissions(session=session, company_id=company.id)
    assert [item.id for item in submissions] == [submission.id]
    audit_actions = {
        audit.action
        for audit in session.execute(select(AuditLog).where(AuditLog.company_id == company.id))
        .scalars()
        .all()
    }
    assert {"transmitted", "elster_transmitted"}.issubset(audit_actions)


def test_elster_transport_adapters(session: Session) -> None:
    company = _seed(session)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )
    payload_hash = "a" * 64

    mock_result = MockElsterTransport().transmit(
        payload_xml="<xml />", payload_hash=payload_hash, vat_return=vat_return
    )
    assert mock_result.status == "transmitted"
    assert mock_result.transfer_ticket.startswith("MOCK-USTVA-2026-05-")

    with pytest.raises(ElsterError, match="ERiC library"):
        EricElsterTransport(eric_library_path=None, certificate_path=None).transmit(
            payload_xml="<xml />", payload_hash=payload_hash, vat_return=vat_return
        )


def test_elster_mock_rejects_production(session: Session) -> None:
    company = _seed(session)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )

    with pytest.raises(ElsterError, match="test environment"):
        submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="production",
            transport="mock",
            changed_by="pytest",
        )


def test_elster_failed_submission_is_recorded(session: Session) -> None:
    company = _seed(session)
    vat_return = save_vat_return(
        session=session,
        company_id=company.id,
        period_label="2026-05",
        changed_by="pytest",
    )

    with pytest.raises(ElsterError, match="recorded as failed"):
        submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="production",
            transport="eric",
            changed_by="pytest",
            config={},
        )

    session.refresh(vat_return)
    assert vat_return.status == "erstellt"
    submissions = list_elster_submissions(session=session, company_id=company.id)
    assert len(submissions) == 1
    failed = submissions[0]
    assert failed.status == "failed"
    assert failed.transport == "eric"
    assert failed.submitted_at is None
    assert "ERiC library" in failed.response_protocol
    assert "<Period>2026-05</Period>" in failed.payload_xml
    assert list_elster_submissions(
        session=session,
        company_id=company.id,
        status="failed",
        transport="eric",
        environment="production",
    ) == [failed]
    assert (
        list_elster_submissions(
            session=session,
            company_id=company.id,
            status="transmitted",
        )
        == []
    )
    summary = elster_submission_summary(session=session, company_id=company.id)
    assert summary["total"] == 1
    assert summary["by_status"]["failed"] == 1
    assert summary["by_transport"]["eric"] == 1
    assert summary["by_environment"]["production"] == 1
    assert summary["latest"]["id"] == failed.id

    with pytest.raises(ElsterError, match="recorded as failed"):
        retry_elster_submission(
            session=session,
            submission_id=failed.id,
            changed_by="pytest",
            config={},
        )
    retried_failures = list_elster_submissions(
        session=session,
        company_id=company.id,
        status="failed",
        transport="eric",
        environment="production",
    )
    assert len(retried_failures) == 2

    audit_actions = {
        item.action
        for item in session.scalars(
            select(AuditLog).where(AuditLog.company_id == company.id)
        )
    }
    assert "failed" in audit_actions
    assert "elster_transmitted" not in audit_actions


def test_elster_readiness_reports_configured_production(tmp_path: Path) -> None:
    eric_library = tmp_path / "libericapi.dylib"
    certificate = tmp_path / "certificate.pfx"
    eric_library.write_text("fake", encoding="utf-8")
    certificate.write_text("fake", encoding="utf-8")

    readiness = elster_readiness(
        {
            "ELSTER_ENVIRONMENT": "production",
            "ELSTER_ERIC_LIBRARY_PATH": str(eric_library),
            "ELSTER_CERTIFICATE_PATH": str(certificate),
            "ELSTER_CERTIFICATE_ALIAS": "prod-cert",
        }
    )
    assert readiness["production_ready"] is True
    assert readiness["eric_transport_available"] is True
    assert readiness["certificate_alias"] == "prod-cert"
    assert readiness["warnings"] == []


def test_elster_api_submits_vat_return(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company = _seed(session)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        vat_return = save_vat_return(
            session=session,
            company_id=company.id,
            period_label="2026-05",
            changed_by="pytest",
        )
        vat_return_id = vat_return.id

    client = app.test_client()
    response = client.post(
        "/api/v1/elster/ustva/submit",
        json={"vat_return_id": vat_return_id, "environment": "test", "transport": "mock"},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["status"] == "transmitted"
    assert payload["transfer_ticket"].startswith("MOCK-USTVA-2026-05-")
    assert payload["payload_hash_valid"] is True

    listed = client.get(
        "/api/v1/elster/submissions",
        query_string={"company_id": payload["company_id"], "vat_return_id": vat_return_id},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.get_json()["submissions"]] == [payload["id"]]
    assert listed.get_json()["filters"]["vat_return_id"] == vat_return_id

    filtered = client.get(
        "/api/v1/elster/submissions",
        query_string={
            "company_id": payload["company_id"],
            "status": "transmitted",
            "transport": "mock",
            "environment": "test",
        },
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.get_json()["submissions"]] == [payload["id"]]

    summary_response = client.get(
        "/api/v1/elster/submissions/summary",
        query_string={"company_id": payload["company_id"]},
    )
    assert summary_response.status_code == 200
    summary = summary_response.get_json()
    assert summary["total"] == 1
    assert summary["by_status"]["transmitted"] == 1
    assert summary["by_transport"]["mock"] == 1
    assert summary["by_environment"]["test"] == 1
    assert summary["latest"]["id"] == payload["id"]

    invalid_filter = client.get(
        "/api/v1/elster/submissions",
        query_string={"company_id": payload["company_id"], "status": "bogus"},
    )
    assert invalid_filter.status_code == 400
    assert "status must" in invalid_filter.get_json()["error"]

    detail = client.get(f"/api/v1/elster/submissions/{payload['id']}")
    assert detail.status_code == 200
    detail_payload = detail.get_json()
    assert detail_payload["id"] == payload["id"]
    assert detail_payload["payload_hash"] == payload["payload_hash"]
    assert detail_payload["payload_hash_valid"] is True
    assert "<Period>2026-05</Period>" in detail_payload["payload_xml"]

    payload_download = client.get(f"/api/v1/elster/submissions/{payload['id']}/payload.xml")
    assert payload_download.status_code == 200
    assert payload_download.content_type == "application/xml; charset=utf-8"
    assert "elster-ustva-2026-05-" in payload_download.headers["Content-Disposition"]
    assert b"<Period>2026-05</Period>" in payload_download.data


def test_elster_api_retries_failed_submission(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company = _seed(session)
        vat_return = save_vat_return(
            session=session,
            company_id=company.id,
            period_label="2026-05",
            changed_by="pytest",
        )
        with pytest.raises(ElsterError, match="recorded as failed"):
            submit_vat_return(
                session=session,
                vat_return_id=vat_return.id,
                environment="production",
                transport="eric",
                changed_by="pytest",
                config={},
            )
        failed_submission_id = list_elster_submissions(
            session=session, company_id=company.id
        )[0].id
        company_id = company.id

    client = app.test_client()
    response = client.post(f"/api/v1/elster/submissions/{failed_submission_id}/retry")

    assert response.status_code == 422
    assert "recorded as failed" in response.get_json()["error"]
    with app.extensions["db_session_factory"]() as session:
        failed_submissions = list_elster_submissions(
            session=session,
            company_id=company_id,
            status="failed",
            transport="eric",
            environment="production",
        )
        assert len(failed_submissions) == 2


def test_elster_submission_history_is_visible_on_ustva_page(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company = _seed(session)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        vat_return = save_vat_return(
            session=session,
            company_id=company.id,
            period_label="2026-05",
            changed_by="pytest",
        )
        first_submission = submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="test",
            transport="mock",
            changed_by="pytest",
        )
        submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="test",
            transport="mock",
            changed_by="pytest",
        )
        company_id = company.id
        submission_id = first_submission.id

    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    response = client.get("/ustva", query_string={"company_id": company_id})

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Übermittlungen:" in html
    assert "ok 2" in html
    assert "2 Übermittlungen" in html
    assert "ELSTER mock transport accepted UStVA 2026-05" in html
    assert html.count("MOCK-USTVA-2026-05-") == 3
    assert "Hash ok" in html
    assert "Payload-XML" in html

    download = client.get(f"/ustva/elster-submissions/{submission_id}/payload.xml")
    assert download.status_code == 200
    assert download.content_type == "application/xml; charset=utf-8"
    assert b"<Period>2026-05</Period>" in download.data


def test_elster_submission_history_filters_and_retries_failed(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    with app.extensions["db_session_factory"]() as session:
        company = _seed(session)
        session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        vat_return = save_vat_return(
            session=session,
            company_id=company.id,
            period_label="2026-05",
            changed_by="pytest",
        )
        with pytest.raises(ElsterError, match="recorded as failed"):
            submit_vat_return(
                session=session,
                vat_return_id=vat_return.id,
                environment="production",
                transport="eric",
                changed_by="pytest",
                config={},
            )
        company_id = company.id
        failed_submission_id = list_elster_submissions(
            session=session, company_id=company.id
        )[0].id

    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    response = client.get(
        "/ustva",
        query_string={
            "company_id": company_id,
            "elster_status": "failed",
            "elster_transport": "eric",
            "elster_environment": "production",
        },
    )

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "fehlgeschlagen 1" in html
    assert "failed" in html
    assert "Retry" in html
    assert "production" in html
    assert "eric" in html

    retry = client.post(
        f"/ustva/elster-submissions/{failed_submission_id}/retry",
        follow_redirects=False,
    )
    assert retry.status_code == 302

    with app.extensions["db_session_factory"]() as session:
        failed_submissions = list_elster_submissions(
            session=session,
            company_id=company_id,
            status="failed",
            transport="eric",
            environment="production",
        )
        assert len(failed_submissions) == 2


def test_elster_readiness_api(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_elster_ready.db'}",
            "ELSTER_ENVIRONMENT": "production",
        }
    )
    client = app.test_client()

    response = client.get("/api/v1/elster/readiness")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["environment"] == "production"
    assert payload["production_ready"] is False
    assert "Production ELSTER requires ERiC library and certificate." in payload["warnings"]
