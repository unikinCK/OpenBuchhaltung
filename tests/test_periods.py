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

    assert carry.source == "year_end_close"

    # Der Ergebnisvortrag verfälscht die GuV nicht: Das Jahresergebnis bleibt
    # 100 — auch mit Abschlussbuchungen, denn der Vortrag stellt nur Konten glatt.
    from app.services.reports import income_statement_for_company, trial_balance_for_company

    totals = income_statement_for_company(session=session, company_id=company.id)["totals"]
    assert totals["net_income"] == Decimal("100.00")
    totals_incl = income_statement_for_company(
        session=session, company_id=company.id, include_closing_entries=True
    )["totals"]
    assert totals_incl["net_income"] == Decimal("100.00")

    # SuSa: ohne Abschlussbuchungen Saldo −100 auf 8400, mit Abschluss glatt.
    susa = {row["code"]: row for row in trial_balance_for_company(
        session=session, company_id=company.id
    )}
    assert susa["8400"]["balance"] == Decimal("-100.00")
    assert "0860" not in susa
    susa_incl = {row["code"]: row for row in trial_balance_for_company(
        session=session, company_id=company.id, include_closing_entries=True
    )}
    assert susa_incl["8400"]["debit_total"] == Decimal("100.00")
    assert susa_incl["8400"]["balance"] == Decimal("0.00")
    assert susa_incl["0860"]["balance"] == Decimal("-100.00")


def test_close_fiscal_year_carries_balances_into_next_year(session: Session) -> None:
    from app.services.reports import balance_sheet_for_company, trial_balance_for_company
    from domain.models import JournalEntry

    company, fiscal_year, _ = _seed_company_with_entry(session)
    result = close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")

    opening = result.opening_balance_entry
    assert opening is not None
    assert opening.source == "carryforward"
    assert opening.entry_date == date(2027, 1, 1)
    assert opening.description == "Saldovortrag aus 2026"
    next_year = session.get(FiscalYear, opening.fiscal_year_id)
    assert next_year.label == "2027" and next_year.is_closed is False
    assert session.get(Period, opening.period_id).period_number == 1
    by_code = {
        session.get(Account, line.account_id).code: line
        for line in session.execute(
            select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == opening.id)
        ).scalars()
    }
    # Bank 100 im Soll, Gewinnvortrag (nach Ergebnisvortrag) 100 im Haben; kein Erlöskonto.
    assert set(by_code) == {"1200", "0860"}
    assert by_code["1200"].debit_amount == Decimal("100.00")
    assert by_code["0860"].credit_amount == Decimal("100.00")

    # SuSa des Folgejahres zeigt die EB-Werte; kumuliert ohne Startdatum nicht doppelt.
    susa_2027 = {row["code"]: row for row in trial_balance_for_company(
        session=session, company_id=company.id, date_from=date(2027, 1, 1)
    )}
    assert susa_2027["1200"]["debit_total"] == Decimal("100.00")
    susa_all = {row["code"]: row for row in trial_balance_for_company(
        session=session, company_id=company.id
    )}
    assert susa_all["1200"]["debit_total"] == Decimal("100.00")

    # Bilanz zum 31.12.2026: Jahresergebnis als eigene Position, Gewinnvortrag noch 0.
    closing_bs = balance_sheet_for_company(
        session=session, company_id=company.id, date_to=date(2026, 12, 31)
    )
    passiva = {row["code"]: row["amount"] for row in closing_bs["liabilities_and_equity"]}
    assert passiva == {"P&L": Decimal("100.00")}
    assert closing_bs["totals"]["is_balanced"] is True
    assert closing_bs["period"]["fiscal_year"] == "2026"

    # Bilanz im Folgejahr: Ergebnis im Gewinnvortrag, kein doppelter Saldovortrag.
    next_bs = balance_sheet_for_company(
        session=session, company_id=company.id, date_to=date(2027, 6, 30)
    )
    assert {row["code"]: row["amount"] for row in next_bs["assets"]} == {
        "1200": Decimal("100.00")
    }
    assert {row["code"]: row["amount"] for row in next_bs["liabilities_and_equity"]} == {
        "0860": Decimal("100.00")
    }
    assert next_bs["totals"]["is_balanced"] is True

    # Vortragsbuchungen sind nicht stornierbar.
    from app.services.journal_entries import reverse_journal_entry

    for entry_id in (opening.id, result.carryforward_entry.id):
        with pytest.raises(JournalEntryCreationError, match="Vortragsbuchung"):
            reverse_journal_entry(
                session=session,
                journal_entry_id=entry_id,
                reversal_date=date(2027, 1, 2),
                changed_by="admin",
            )
    assert session.execute(
        select(JournalEntry.id).where(JournalEntry.source == "storno")
    ).first() is None


def test_closing_period_entries_are_excluded_by_default(session: Session) -> None:
    from app.services.income_taxes import compute_income_tax_return
    from app.services.reports import balance_sheet_for_company, income_statement_for_company

    company, fiscal_year, _ = _seed_company_with_entry(session)
    afa_account = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="4830",
        name="Abschreibungen",
        account_type="expense",
    )
    session.add(afa_account)
    session.commit()
    bank_id = session.execute(select(Account.id).where(Account.code == "1200")).scalar_one()
    # Abschlussbuchung in Periode 13 (wie eine AfA): 30 Aufwand.
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 12, 31),
            description="AfA 2026",
            status="posted",
            post_to_closing_period=True,
            lines=[
                JournalLineInput(afa_account.id, Decimal("30.00"), Decimal("0.00")),
                JournalLineInput(bank_id, Decimal("0.00"), Decimal("30.00")),
            ],
        ),
    )

    def net_income(include: bool) -> Decimal:
        return income_statement_for_company(
            session=session,
            company_id=company.id,
            date_from=date(2026, 1, 1),
            date_to=date(2026, 12, 31),
            include_closing_entries=include,
        )["totals"]["net_income"]

    assert net_income(False) == Decimal("100.00")
    assert net_income(True) == Decimal("70.00")

    close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")
    # Nach dem Abschluss unverändert — der Ergebnisvortrag zählt nie mit.
    assert net_income(False) == Decimal("100.00")
    assert net_income(True) == Decimal("70.00")

    taxable = {
        include: compute_income_tax_return(
            session=session,
            company_id=company.id,
            year=2026,
            tax_type="corporate_income",
            include_closing_entries=include,
        )["basis"]["net_income"]
        for include in (False, True)
    }
    assert taxable == {False: "100.00", True: "70.00"}

    bs_incl = balance_sheet_for_company(
        session=session,
        company_id=company.id,
        date_to=date(2026, 12, 31),
        include_closing_entries=True,
    )
    assert {row["code"]: row["amount"] for row in bs_incl["assets"]} == {
        "1200": Decimal("70.00")
    }
    assert {row["code"]: row["amount"] for row in bs_incl["liabilities_and_equity"]} == {
        "P&L": Decimal("70.00")
    }
    assert bs_incl["totals"]["is_balanced"] is True


def test_close_fiscal_year_requires_previous_years_closed(session: Session) -> None:
    company = _seed_company(session)
    session.add(
        Account(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code="0860",
            name="Gewinnvortrag vor Verwendung",
            account_type="equity",
        )
    )
    session.commit()
    _book(session, company, date(2025, 6, 1))
    _book(session, company, date(2026, 6, 1))
    years = {
        year.label: year for year in session.execute(select(FiscalYear)).scalars().all()
    }

    with pytest.raises(PeriodActionError, match="zeitlicher Reihenfolge.*2025"):
        close_fiscal_year(session=session, fiscal_year_id=years["2026"].id, changed_by="admin")

    first = close_fiscal_year(session=session, fiscal_year_id=years["2025"].id, changed_by="admin")
    assert first.opening_balance_entry.entry_date == date(2026, 1, 1)
    second = close_fiscal_year(
        session=session, fiscal_year_id=years["2026"].id, changed_by="admin"
    )
    # Saldovortrag 2027 kumuliert beide Jahre: Bank 200 / Gewinnvortrag 200.
    amounts = {
        session.get(Account, line.account_id).code: line.debit_amount or -line.credit_amount
        for line in session.execute(
            select(JournalEntryLine).where(
                JournalEntryLine.journal_entry_id == second.opening_balance_entry.id
            )
        ).scalars()
    }
    assert amounts == {"1200": Decimal("200.00"), "0860": Decimal("-200.00")}


def test_close_fiscal_year_is_atomic(session: Session, monkeypatch) -> None:
    from app.services import periods as periods_module
    from domain.models import JournalEntry

    company, fiscal_year, _ = _seed_company_with_entry(session)

    def failing_audit(**kwargs):
        raise RuntimeError("Audit-Log nicht erreichbar")

    monkeypatch.setattr(periods_module, "log_audit_event", failing_audit)
    with pytest.raises(RuntimeError):
        close_fiscal_year(session=session, fiscal_year_id=fiscal_year.id, changed_by="admin")
    session.rollback()

    # Weder Vortragsbuchungen noch Sperren noch ein Folgejahr bleiben zurück.
    assert session.execute(
        select(JournalEntry.id).where(
            JournalEntry.source.in_(["year_end_close", "carryforward"])
        )
    ).first() is None
    assert session.execute(select(PeriodLock.id)).first() is None
    assert session.execute(select(FiscalYear.label)).scalars().all() == ["2026"]
    assert session.get(FiscalYear, fiscal_year.id).is_closed is False

    monkeypatch.undo()
    result = close_fiscal_year(
        session=session, fiscal_year_id=fiscal_year.id, changed_by="admin"
    )
    assert result.fiscal_year.is_closed is True
    assert result.opening_balance_entry is not None


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


def test_periods_api_lifecycle(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Perioden API", "company_name": "Perioden API GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "0860",
            "name": "Gewinnvortrag vor Verwendung",
            "account_type": "equity",
        },
    )

    start_response = client.post(
        "/api/v1/companies/1/fiscal-year-start",
        json={"start_month": 4},
    )
    assert start_response.status_code == 200
    assert start_response.get_json()["fiscal_year_start_month"] == 4

    create_response = client.post(
        "/api/v1/fiscal-years",
        json={
            "company_id": 1,
            "label": "2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        },
    )
    assert create_response.status_code == 201
    fiscal_year = create_response.get_json()
    fiscal_year_id = fiscal_year["id"]
    period_id = fiscal_year["periods"][0]["id"]

    list_response = client.get("/api/v1/fiscal-years", query_string={"company_id": 1})
    assert list_response.status_code == 200
    assert list_response.get_json()["fiscal_years"][0]["label"] == "2026"

    lock_response = client.post(
        f"/api/v1/periods/{period_id}/lock",
        json={"reason": "Monatsabschluss"},
    )
    assert lock_response.status_code == 200
    assert lock_response.get_json()["status"] == "locked"

    unlock_response = client.post(f"/api/v1/periods/{period_id}/unlock", json={})
    assert unlock_response.status_code == 200
    assert unlock_response.get_json()["status"] == "open"

    close_response = client.post(f"/api/v1/fiscal-years/{fiscal_year_id}/close", json={})
    assert close_response.status_code == 200
    assert close_response.get_json()["is_closed"] is True
    assert {period["status"] for period in close_response.get_json()["periods"]} == {"locked"}


def test_reports_api_exposes_closing_switch_and_carryforward(tmp_path: Path) -> None:
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Abschluss API", "company_name": "Abschluss API GmbH"},
    )
    for code, name, account_type in (
        ("1200", "Bank", "asset"),
        ("8400", "Erlöse", "income"),
        ("0860", "Gewinnvortrag vor Verwendung", "equity"),
    ):
        client.post(
            "/api/v1/accounts",
            json={"company_id": 1, "code": code, "name": name, "account_type": account_type},
        )
    client.post(
        "/api/v1/journal-entries",
        json={
            "company_id": 1,
            "entry_date": "2026-05-10",
            "description": "Erlös",
            "lines": [
                {"account_code": "1200", "debit_amount": "100.00"},
                {"account_code": "8400", "credit_amount": "100.00"},
            ],
        },
    )
    fiscal_year_id = client.get(
        "/api/v1/fiscal-years", query_string={"company_id": 1}
    ).get_json()["fiscal_years"][0]["id"]

    closed = client.post(f"/api/v1/fiscal-years/{fiscal_year_id}/close", json={})
    assert closed.status_code == 200
    assert closed.get_json()["carryforward_entry_id"] is not None
    assert closed.get_json()["opening_balance_entry_id"] is not None

    income = client.get("/api/v1/income-statement", query_string={"company_id": 1}).get_json()
    assert income["totals"]["net_income"] == "100.00"
    assert income["period"]["include_closing_entries"] is False

    susa_incl = client.get(
        "/api/v1/trial-balance",
        query_string={"company_id": 1, "include_closing_entries": "true"},
    ).get_json()
    assert susa_incl["period"]["include_closing_entries"] is True
    assert {row["code"]: row["balance"] for row in susa_incl["rows"]} == {
        "0860": "-100.00",
        "1200": "100.00",
        "8400": "0.00",
    }

    balance = client.get(
        "/api/v1/balance-sheet", query_string={"company_id": 1, "date_to": "2027-06-30"}
    ).get_json()
    assert balance["period"]["fiscal_year"] == "2027"
    assert balance["totals"]["is_balanced"] is True
    assert {row["code"]: row["amount"] for row in balance["liabilities_and_equity"]} == {
        "0860": "100.00"
    }

    csv_export = client.get(
        "/api/v1/exports/trial-balance.csv",
        query_string={"company_id": 1, "date_from": "2027-01-01"},
    )
    assert csv_export.status_code == 200
    assert "1200,Bank,100.00,0.00,100.00" in csv_export.get_data(as_text=True)

    ui = app.test_client()
    _login(ui, "admin", "admin123")
    page = ui.get("/berichte?company_id=1&include_closing_entries=1")
    assert page.status_code == 200
    assert b"inkl. Abschlussbuchungen" in page.data
