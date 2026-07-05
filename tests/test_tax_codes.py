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
