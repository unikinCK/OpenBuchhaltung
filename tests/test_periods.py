from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.periods import (
    PeriodActionError,
    close_fiscal_year,
    create_fiscal_year,
    lock_period,
    set_fiscal_year_start_month,
    unlock_period,
)
from domain.models import (
    Account,
    AuditLog,
    Base,
    Company,
    FiscalYear,
    JournalEntryLine,
    Period,
    PeriodLock,
    Tenant,
    User,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company_with_entry(session: Session) -> tuple[Company, FiscalYear, Period]:
    tenant = Tenant(name="Perioden Tenant")
    company = Company(tenant=tenant, name="Perioden GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    revenue = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="income",
    )
    retained = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="0860",
        name="Gewinnvortrag vor Verwendung",
        account_type="equity",
    )
    session.add_all([bank, revenue, retained])
    session.commit()

    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 3, 15),
            description="Erste Buchung",
            status="posted",
            lines=[
                JournalLineInput(bank.id, Decimal("100.00"), Decimal("0.00")),
                JournalLineInput(revenue.id, Decimal("0.00"), Decimal("100.00")),
            ],
        ),
    )
    fiscal_year = session.execute(select(FiscalYear)).scalar_one()
    period = session.execute(
        select(Period).where(
            Period.is_closing.is_(False),
            Period.start_date <= date(2026, 3, 15),
            Period.end_date >= date(2026, 3, 15),
        )
    ).scalar_one()
    return company, fiscal_year, period


def _seed_company(session: Session, *, fiscal_year_start_month: int = 1) -> Company:
    tenant = Tenant(name="WJ Tenant")
    company = Company(
        tenant=tenant,
        name="WJ GmbH",
        currency_code="EUR",
        fiscal_year_start_month=fiscal_year_start_month,
    )
    session.add_all([tenant, company])
    session.flush()
    session.add_all(
        [
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="1200",
                name="Bank",
                account_type="asset",
            ),
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code="8400",
                name="Erlöse",
                account_type="income",
            ),
        ]
    )
    session.commit()
    return company


def _book(session: Session, company: Company, entry_date, *, closing: bool = False):
    bank, revenue = (
        session.execute(
            select(Account.id)
            .where(Account.company_id == company.id, Account.code.in_(["1200", "8400"]))
            .order_by(Account.code)
        )
        .scalars()
        .all()
    )
    return create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=entry_date,
            description="Buchung",
            status="posted",
            lines=[
                JournalLineInput(bank, Decimal("100.00"), Decimal("0.00")),
                JournalLineInput(revenue, Decimal("0.00"), Decimal("100.00")),
            ],
            post_to_closing_period=closing,
        ),
    )


def test_deviating_fiscal_year_auto_created_from_start_month(session: Session) -> None:
    company = _seed_company(session, fiscal_year_start_month=7)

    _book(session, company, date(2026, 8, 15))

    fiscal_year = session.execute(select(FiscalYear)).scalar_one()
    assert fiscal_year.start_date == date(2026, 7, 1)
    assert fiscal_year.end_date == date(2027, 6, 30)
    assert fiscal_year.label == "2026/2027"

    regular = (
        session.execute(
            select(Period)
            .where(Period.is_closing.is_(False))
            .order_by(Period.period_number)
        )
        .scalars()
        .all()
    )
    assert len(regular) == 12
    assert regular[0].start_date == date(2026, 7, 1)
    assert regular[-1].end_date == date(2027, 6, 30)

    closing = session.execute(
        select(Period).where(Period.is_closing.is_(True))
    ).scalar_one()
    assert closing.period_number == 13
    assert closing.start_date == closing.end_date == date(2027, 6, 30)

    # Ein Datum vor dem Beginn-Monat gehört zum vorangehenden Geschäftsjahr.
    _book(session, company, date(2026, 3, 1))
    labels = set(session.execute(select(FiscalYear.label)).scalars())
    assert labels == {"2026/2027", "2025/2026"}


def test_create_short_fiscal_year_and_closing_booking(session: Session) -> None:
    company = _seed_company(session)

    fiscal_year = create_fiscal_year(
        session=session,
        company_id=company.id,
        label="2026 (Rumpf)",
        start_date=date(2026, 5, 15),
        end_date=date(2026, 12, 31),
        changed_by="admin",
    )

    regular = (
        session.execute(
            select(Period)
            .where(Period.fiscal_year_id == fiscal_year.id, Period.is_closing.is_(False))
            .order_by(Period.period_number)
        )
        .scalars()
        .all()
    )
    assert len(regular) == 8  # Mai (Teilmonat) bis Dezember
    assert regular[0].start_date == date(2026, 5, 15)
    assert regular[0].end_date == date(2026, 5, 31)

    entry = _book(session, company, date(2026, 12, 31), closing=True)
    booked_period = session.get(Period, entry.period_id)
    assert booked_period.is_closing is True

    # Eine reguläre Buchung am selben Tag landet in der Dezember-Periode, nicht im Abschluss.
    regular_entry = _book(session, company, date(2026, 12, 31))
    assert session.get(Period, regular_entry.period_id).is_closing is False


def test_create_fiscal_year_rejects_overlap(session: Session) -> None:
    company = _seed_company(session)
    create_fiscal_year(
        session=session,
        company_id=company.id,
        label="2026",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        changed_by="admin",
    )
    with pytest.raises(PeriodActionError, match="überschneidet"):
        create_fiscal_year(
            session=session,
            company_id=company.id,
            label="2026b",
            start_date=date(2026, 6, 1),
            end_date=date(2027, 5, 31),
            changed_by="admin",
        )


def test_set_fiscal_year_start_month_validates_range(session: Session) -> None:
    company = _seed_company(session)
    updated = set_fiscal_year_start_month(
        session=session, company_id=company.id, start_month=4, changed_by="admin"
    )
    assert updated.fiscal_year_start_month == 4

    with pytest.raises(PeriodActionError):
        set_fiscal_year_start_month(
            session=session, company_id=company.id, start_month=13, changed_by="admin"
        )


def test_lock_and_unlock_period_with_audit(session: Session) -> None:
    _, _, period = _seed_company_with_entry(session)

    locked = lock_period(
        session=session, period_id=period.id, locked_by="tester", reason="Monatsabschluss"
    )
    assert locked.status == "locked"
    assert session.execute(select(PeriodLock)).scalar_one().reason == "Monatsabschluss"

    with pytest.raises(PeriodActionError, match="bereits gesperrt"):
        lock_period(session=session, period_id=period.id, locked_by="tester")

    unlocked = unlock_period(session=session, period_id=period.id, changed_by="admin")
    assert unlocked.status == "open"
    assert session.execute(select(PeriodLock)).first() is None

    actions = set(
        session.execute(select(AuditLog.action).where(AuditLog.entity_type == "period")).scalars()
    )
    assert actions == {"locked", "unlocked"}


def test_close_fiscal_year_locks_all_periods_and_blocks_bookings(session: Session) -> None:
    company, fiscal_year, period = _seed_company_with_entry(session)

    result = close_fiscal_year(
        session=session, fiscal_year_id=fiscal_year.id, changed_by="admin"
    )
    assert result.fiscal_year.is_closed is True
    locked_period_ids = set(session.execute(select(PeriodLock.period_id)).scalars())
    assert period.id in locked_period_ids

    with pytest.raises(PeriodActionError, match="bereits abgeschlossen"):
        close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")

    with pytest.raises(PeriodActionError, match="abgeschlossen"):
        unlock_period(session=session, period_id=period.id, changed_by="admin")

    bank_id, revenue_id = (
        session.execute(
            select(Account.id).where(Account.code.in_(["1200", "8400"])).order_by(Account.code)
        )
        .scalars()
        .all()
    )
    with pytest.raises(JournalEntryCreationError, match="Geschäftsjahr ist abgeschlossen"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 8, 1),
                description="Nachbuchung",
                status="posted",
                lines=[
                    JournalLineInput(bank_id, Decimal("50.00"), Decimal("0.00")),
                    JournalLineInput(revenue_id, Decimal("0.00"), Decimal("50.00")),
                ],
            ),
        )


def test_close_fiscal_year_books_result_carryforward(session: Session) -> None:
    company, fiscal_year, _ = _seed_company_with_entry(session)

    result = close_fiscal_year(
        session=session, fiscal_year_id=fiscal_year.id, changed_by="admin"
    )

    carry = result.carryforward_entry
    assert carry is not None
    assert carry.description == "Ergebnisvortrag 2026"

    lines = (
        session.execute(
            select(JournalEntryLine)
            .where(JournalEntryLine.journal_entry_id == carry.id)
            .order_by(JournalEntryLine.line_number)
        )
        .scalars()
        .all()
    )
    by_code = {
        session.get(Account, line.account_id).code: line for line in lines
    }
    # Erlöse (100 Haben) werden im Soll glattgestellt, Gewinnvortrag erhält 100 Haben
    assert by_code["8400"].debit_amount == Decimal("100.00")
    assert by_code["0860"].credit_amount == Decimal("100.00")

    # Nach dem Vortrag ist der GuV-Saldo im Geschäftsjahr null
    from app.services.reports import income_statement_for_company

    totals = income_statement_for_company(session=session, company_id=company.id)["totals"]
    assert totals["net_income"] == Decimal("0.00")


def test_close_fiscal_year_without_retained_account_fails(session: Session) -> None:
    _, fiscal_year, _ = _seed_company_with_entry(session)
    retained = session.execute(
        select(Account).where(Account.code == "0860")
    ).scalar_one()
    session.delete(retained)
    session.commit()

    with pytest.raises(PeriodActionError, match="Gewinnvortragskonto"):
        close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_periods.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add_all(
            [
                User(
                    username="admin",
                    password_hash=hash_password("admin123"),
                    role="Admin",
                    tenant_id=None,
                ),
                User(
                    username="buchhalter",
                    password_hash=hash_password("buch123"),
                    role="Buchhalter",
                    tenant_id=None,
                ),
            ]
        )
        db_session.commit()
    return app


def _login(client, username: str, password: str) -> None:
    client.post("/auth/login", data={"username": username, "password": password})


def _seed_ui_booking(client) -> None:
    client.post("/tenants", data={"tenant_name": "P Mandant", "company_name": "P GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "8400", "name": "Erlöse", "account_type": "income"},
    )
    client.post(
        "/accounts",
        data={
            "company_id": "1",
            "code": "0860",
            "name": "Gewinnvortrag vor Verwendung",
            "account_type": "equity",
        },
    )
    client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-05-10",
            "description": "UI Buchung",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "100.00",
        },
    )


def test_periods_page_shows_fiscal_year_and_lock_flow(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    _login(client, "admin", "admin123")
    _seed_ui_booking(client)

    page = client.get("/perioden?company_id=1")
    assert page.status_code == 200
    assert "Geschäftsjahr 2026".encode() in page.data

    lock_response = client.post(
        "/perioden/1/sperren",
        data={"company_id": "1", "reason": "Test"},
        follow_redirects=True,
    )
    assert lock_response.status_code == 200
    assert b"wurde gesperrt" in lock_response.data
    assert b"Entsperren" in lock_response.data

    unlock_response = client.post(
        "/perioden/1/entsperren", data={"company_id": "1"}, follow_redirects=True
    )
    assert unlock_response.status_code == 200
    assert b"wurde entsperrt" in unlock_response.data


def test_unlock_and_year_close_require_admin(tmp_path):
    app = _create_ui_app(tmp_path)
    admin_client = app.test_client()
    _login(admin_client, "admin", "admin123")
    _seed_ui_booking(admin_client)
    admin_client.post("/perioden/1/sperren", data={"company_id": "1"})

    buchhalter_client = app.test_client()
    _login(buchhalter_client, "buchhalter", "buch123")

    unlock_response = buchhalter_client.post(
        "/perioden/1/entsperren", data={"company_id": "1"}, follow_redirects=True
    )
    assert b"nur ein Administrator" in unlock_response.data

    close_response = buchhalter_client.post(
        "/geschaeftsjahre/1/abschliessen", data={"company_id": "1"}, follow_redirects=True
    )
    assert b"nur ein Administrator" in close_response.data


def test_admin_can_close_fiscal_year_via_ui(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    _login(client, "admin", "admin123")
    _seed_ui_booking(client)

    close_response = client.post(
        "/geschaeftsjahre/1/abschliessen", data={"company_id": "1"}, follow_redirects=True
    )
    assert close_response.status_code == 200
    assert b"wurde abgeschlossen" in close_response.data
    assert b"abgeschlossen</span>" in close_response.data

    booking_response = client.post(
        "/journal-entries",
        data={
            "company_id": "1",
            "entry_date": "2026-06-01",
            "description": "Nach Abschluss",
            "debit_account_id": "1",
            "credit_account_id": "2",
            "amount": "10.00",
        },
        follow_redirects=True,
    )
    assert "Geschäftsjahr ist abgeschlossen".encode() in booking_response.data
