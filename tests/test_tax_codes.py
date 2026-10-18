from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import Account, Base, Company, JournalEntryLine, TaxCode, Tenant


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> Company:
    tenant = Tenant(name="Steuer Tenant")
    company = Company(tenant=tenant, name="Steuer GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    for code, name, account_type in (
        ("1400", "Forderungen", "asset"),
        ("1776", "Umsatzsteuer 19 %", "liability"),
        ("1576", "Vorsteuer 19 %", "asset"),
        ("1571", "Vorsteuer 7 %", "asset"),
        ("1771", "Umsatzsteuer 7 %", "liability"),
        ("8400", "Erlöse 19 % USt", "income"),
    ):
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
            )
        )
    session.commit()
    return company


def _account_id(session: Session, company: Company, code: str) -> int:
    return session.execute(
        select(Account.id).where(Account.company_id == company.id, Account.code == code)
    ).scalar_one()


def test_ensure_default_tax_codes_is_idempotent(session: Session) -> None:
    company = _seed_company(session)

    first_run = ensure_default_tax_codes(session=session, company=company)
    second_run = ensure_default_tax_codes(session=session, company=company)

    assert first_run == 5
    assert second_run == 0

    ust19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    assert ust19.rate == Decimal("19.00")
    assert ust19.vat_account_id == _account_id(session, company, "1776")


def test_journal_entry_expands_tax_line_for_revenue(session: Session) -> None:
    company = _seed_company(session)
    ensure_default_tax_codes(session=session, company=company)
    ust19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 5),
            description="Ausgangsrechnung mit USt",
            status="posted",
            lines=[
                JournalLineInput(
                    account_id=_account_id(session, company, "1400"),
                    debit_amount=Decimal("1190.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "8400"),
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("1000.00"),
                    tax_code_id=ust19.id,
                ),
            ],
        ),
    )

    lines = session.execute(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == entry.id)
        .order_by(JournalEntryLine.line_number)
    ).scalars().all()

    assert len(lines) == 3
    tax_line = lines[2]
    assert tax_line.account_id == _account_id(session, company, "1776")
    assert tax_line.credit_amount == Decimal("190.00")
    assert tax_line.tax_code_id == ust19.id


def test_journal_entry_rejects_tax_code_without_vat_account(session: Session) -> None:
    company = _seed_company(session)
    broken_tax_code = TaxCode(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="Kaputt19",
        rate=Decimal("19.00"),
        description="ohne Konto",
        vat_account_id=None,
    )
    session.add(broken_tax_code)
    session.commit()

    with pytest.raises(JournalEntryCreationError, match="kein Steuerkonto"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 7, 5),
                description="Kaputter Steuercode",
                status="posted",
                lines=[
                    JournalLineInput(
                        account_id=_account_id(session, company, "1400"),
                        debit_amount=Decimal("119.00"),
                        credit_amount=Decimal("0.00"),
                    ),
                    JournalLineInput(
                        account_id=_account_id(session, company, "8400"),
                        debit_amount=Decimal("0.00"),
                        credit_amount=Decimal("100.00"),
                        tax_code_id=broken_tax_code.id,
                    ),
                ],
            ),
        )


def test_zero_rate_tax_code_adds_no_line(session: Session) -> None:
    company = _seed_company(session)
    ensure_default_tax_codes(session=session, company=company)
    frei = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "frei")
    ).scalar_one()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 5),
            description="Steuerfreie Buchung",
            status="posted",
            lines=[
                JournalLineInput(
                    account_id=_account_id(session, company, "1400"),
                    debit_amount=Decimal("100.00"),
                    credit_amount=Decimal("0.00"),
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "8400"),
                    debit_amount=Decimal("0.00"),
                    credit_amount=Decimal("100.00"),
                    tax_code_id=frei.id,
                ),
            ],
        ),
    )

    lines = session.execute(
        select(JournalEntryLine).where(JournalEntryLine.journal_entry_id == entry.id)
    ).scalars().all()
    assert len(lines) == 2


def _seed_company_skr04(session: Session) -> Company:
    tenant = Tenant(name="SKR04 Tenant")
    company = Company(tenant=tenant, name="SKR04 GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()
    for code, name, account_type in (
        ("3806", "Umsatzsteuer 19 %", "liability"),
        ("3801", "Umsatzsteuer 7 %", "liability"),
        ("1406", "Abziehbare Vorsteuer 19 %", "asset"),
        ("1401", "Abziehbare Vorsteuer 7 %", "asset"),
    ):
        session.add(
            Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=name,
                account_type=account_type,
            )
        )
    session.commit()
    return company


def test_ensure_default_tax_codes_resolves_skr04_accounts(session: Session) -> None:
    company = _seed_company_skr04(session)
    created = ensure_default_tax_codes(session=session, company=company)
    assert created == 5

    ust19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    vst19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "VSt19")
    ).scalar_one()
    assert ust19.vat_account_id == _account_id(session, company, "3806")
    assert vst19.vat_account_id == _account_id(session, company, "1406")


def test_ensure_default_tax_codes_repairs_missing_vat_account(session: Session) -> None:
    company = _seed_company_skr04(session)
    # Historischer Zustand: Code existiert, aber ohne Steuerkonto (z. B. weil er
    # gegen den falschen Kontenrahmen aufgelöst wurde).
    session.add(
        TaxCode(
            tenant_id=company.tenant_id,
            company_id=company.id,
            code="USt19",
            rate=Decimal("19.00"),
            vat_account_id=None,
        )
    )
    session.commit()

    changed = ensure_default_tax_codes(session=session, company=company)
    # 4 neue Codes + 1 Reparatur.
    assert changed == 5

    ust19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()
    assert ust19.vat_account_id == _account_id(session, company, "3806")

    # Idempotent: zweiter Lauf ändert nichts mehr.
    assert ensure_default_tax_codes(session=session, company=company) == 0


def test_tax_codes_api_list_create_and_defaults(tmp_path) -> None:
    from app import create_app
    from app.auth import hash_password
    from domain.models import User

    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'tax_codes_api.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    client = app.test_client()

    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Steuer API", "company_name": "Steuer API GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "3806",
            "name": "Umsatzsteuer 19 %",
            "account_type": "liability",
        },
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "1406",
            "name": "Abziehbare Vorsteuer 19 %",
            "account_type": "asset",
        },
    )

    defaults = client.post("/api/v1/tax-codes/defaults", json={"company_id": 1})
    assert defaults.status_code == 200
    body = defaults.get_json()
    assert body["changed"] == 5
    by_code = {tc["code"]: tc for tc in body["tax_codes"]}
    assert by_code["USt19"]["vat_account_id"] is not None
    assert by_code["VSt19"]["vat_account_id"] is not None

    listed = client.get("/api/v1/tax-codes?company_id=1")
    assert listed.status_code == 200
    assert len(listed.get_json()["tax_codes"]) == 5

    created = client.post(
        "/api/v1/tax-codes",
        json={
            "company_id": 1,
            "code": "USt19-Sonder",
            "rate": "19.00",
            "vat_account_code": "3806",
            "description": "Sonderfall",
        },
    )
    assert created.status_code == 201
    assert created.get_json()["rate"] == "19.00"

    duplicate = client.post(
        "/api/v1/tax-codes",
        json={"company_id": 1, "code": "USt19-Sonder", "rate": "19.00"},
    )
    assert duplicate.status_code == 409

    bad_account = client.post(
        "/api/v1/tax-codes",
        json={"company_id": 1, "code": "X", "rate": "19.00", "vat_account_code": "9999"},
    )
    assert bad_account.status_code == 422


def test_default_tax_codes_carry_direction(session: Session) -> None:
    company = _seed_company(session)
    ensure_default_tax_codes(session=session, company=company)
    kinds = dict(
        session.execute(
            select(TaxCode.code, TaxCode.kind).where(TaxCode.company_id == company.id)
        ).all()
    )
    assert kinds == {
        "USt19": "output",
        "USt7": "output",
        "VSt19": "input",
        "VSt7": "input",
        "frei": "output",
    }


def _add_expense_account(session: Session, company: Company) -> int:
    account = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="4200",
        name="Raumkosten",
        account_type="expense",
    )
    session.add(account)
    session.commit()
    return account.id


def test_input_tax_code_on_revenue_line_is_rejected(session: Session) -> None:
    # Befund F5: VSt19 auf einer Erlöszeile würde Haben 1576 buchen (Kz 66 sinkt).
    company = _seed_company(session)
    ensure_default_tax_codes(session=session, company=company)
    vst19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "VSt19")
    ).scalar_one()

    with pytest.raises(JournalEntryCreationError, match="Vorsteuer.*nicht zulässig"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 7, 5),
                description="Falscher Steuercode",
                status="posted",
                lines=[
                    JournalLineInput(
                        account_id=_account_id(session, company, "1400"),
                        debit_amount=Decimal("1190.00"),
                    ),
                    JournalLineInput(
                        account_id=_account_id(session, company, "8400"),
                        credit_amount=Decimal("1000.00"),
                        tax_code_id=vst19.id,
                    ),
                ],
            ),
        )
    assert session.execute(select(JournalEntryLine.id)).first() is None


def test_output_tax_code_on_expense_line_is_rejected(session: Session) -> None:
    company = _seed_company(session)
    expense_id = _add_expense_account(session, company)
    ensure_default_tax_codes(session=session, company=company)
    ust19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "USt19")
    ).scalar_one()

    with pytest.raises(JournalEntryCreationError, match="Umsatzsteuer.*nicht zulässig"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 7, 5),
                description="USt auf Aufwand",
                status="posted",
                lines=[
                    JournalLineInput(
                        account_id=expense_id,
                        debit_amount=Decimal("100.00"),
                        tax_code_id=ust19.id,
                    ),
                    JournalLineInput(
                        account_id=_account_id(session, company, "1400"),
                        credit_amount=Decimal("119.00"),
                    ),
                ],
            ),
        )


def test_zero_rate_code_is_allowed_on_any_account(session: Session) -> None:
    company = _seed_company(session)
    expense_id = _add_expense_account(session, company)
    ensure_default_tax_codes(session=session, company=company)
    frei = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "frei")
    ).scalar_one()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 5),
            description="Steuerfreie Eingangsleistung",
            status="posted",
            lines=[
                JournalLineInput(
                    account_id=expense_id, debit_amount=Decimal("100.00"), tax_code_id=frei.id
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "1400"),
                    credit_amount=Decimal("100.00"),
                ),
            ],
        ),
    )
    assert entry.id is not None


def test_explicit_tax_line_must_be_on_base_line_side(session: Session) -> None:
    company = _seed_company(session)
    expense_id = _add_expense_account(session, company)
    ensure_default_tax_codes(session=session, company=company)
    vst19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "VSt19")
    ).scalar_one()

    with pytest.raises(JournalEntryCreationError, match="derselben Seite"):
        create_journal_entry(
            session=session,
            payload=JournalEntryInput(
                company_id=company.id,
                entry_date=date(2026, 7, 5),
                description="Steuerzeile auf falscher Seite",
                status="posted",
                expand_tax_lines=False,
                lines=[
                    JournalLineInput(
                        account_id=expense_id, debit_amount=Decimal("100.00"), tax_code_id=vst19.id
                    ),
                    JournalLineInput(
                        account_id=_account_id(session, company, "1576"),
                        credit_amount=Decimal("19.00"),
                        tax_code_id=vst19.id,
                    ),
                    JournalLineInput(
                        account_id=_account_id(session, company, "1400"),
                        credit_amount=Decimal("81.00"),
                    ),
                ],
            ),
        )

    # Richtig herum (Steuerzeile im Soll wie die Bemessungsgrundlage) geht es durch.
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 5),
            description="Explizite Steuerzeile",
            status="posted",
            expand_tax_lines=False,
            lines=[
                JournalLineInput(
                    account_id=expense_id, debit_amount=Decimal("100.00"), tax_code_id=vst19.id
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "1576"),
                    debit_amount=Decimal("19.00"),
                    tax_code_id=vst19.id,
                ),
                JournalLineInput(
                    account_id=_account_id(session, company, "1400"),
                    credit_amount=Decimal("119.00"),
                ),
            ],
        ),
    )
    assert entry.id is not None


def test_tax_codes_api_kind_is_validated_and_derived(tmp_path) -> None:
    from app import create_app
    from app.auth import hash_password
    from domain.models import User

    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'tax_codes_kind.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    client = app.test_client()
    client.post(
        "/api/v1/tenants",
        json={"tenant_name": "Kind API", "company_name": "Kind API GmbH"},
    )
    client.post(
        "/api/v1/accounts",
        json={"company_id": 1, "code": "1406", "name": "Vorsteuer 19 %", "account_type": "asset"},
    )
    client.post(
        "/api/v1/accounts",
        json={
            "company_id": 1,
            "code": "3806",
            "name": "Umsatzsteuer 19 %",
            "account_type": "liability",
        },
    )

    explicit = client.post(
        "/api/v1/tax-codes",
        json={
            "company_id": 1,
            "code": "VSt19-Import",
            "rate": "19.00",
            "kind": "input",
            "vat_account_code": "1406",
        },
    )
    assert explicit.status_code == 201
    assert explicit.get_json()["kind"] == "input"

    derived_from_account = client.post(
        "/api/v1/tax-codes",
        json={"company_id": 1, "code": "Sonder19", "rate": "19.00", "vat_account_code": "3806"},
    )
    assert derived_from_account.status_code == 201
    assert derived_from_account.get_json()["kind"] == "output"

    derived_from_code = client.post(
        "/api/v1/tax-codes",
        json={"company_id": 1, "code": "VSt0", "rate": "0.00"},
    )
    assert derived_from_code.status_code == 201
    assert derived_from_code.get_json()["kind"] == "input"

    mismatch = client.post(
        "/api/v1/tax-codes",
        json={
            "company_id": 1,
            "code": "Verdreht",
            "rate": "19.00",
            "kind": "output",
            "vat_account_code": "1406",
        },
    )
    assert mismatch.status_code == 422
    assert "Kontoart liability" in mismatch.get_json()["error"]

    unknown = client.post(
        "/api/v1/tax-codes",
        json={"company_id": 1, "code": "Unbekannt", "rate": "19.00", "kind": "sideways"},
    )
    assert unknown.status_code == 422

    listed = client.get("/api/v1/tax-codes?company_id=1").get_json()["tax_codes"]
    assert {item["code"]: item["kind"] for item in listed} == {
        "VSt19-Import": "input",
        "Sonder19": "output",
        "VSt0": "input",
    }
