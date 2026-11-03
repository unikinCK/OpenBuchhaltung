"""Kassenbuch: Einträge, Storno, Kassensturz, Bericht, Prüfung, API, MCP und Oberfläche."""

from __future__ import annotations

import csv
import io
import json
import shutil
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, create_session_factory
from app.services.cash_book import (
    CashBookError,
    account_balance,
    business_today,
    calculate_cash_entry_hash,
    cash_report,
    cash_report_csv,
    create_cash_book,
    create_cash_closing,
    create_cash_entry,
    current_balance,
    reverse_cash_entry,
    update_cash_book,
    verify_cash_book_integrity,
)
from app.services.chat import chat_tool_blocked, chat_tool_requires_confirmation
from app.services.journal_entries import (
    JournalEntryCreationError,
    JournalEntryInput,
    JournalLineInput,
    amend_journal_entry,
    create_journal_entry,
    finalize_journal_entries_until,
    reverse_journal_entry,
)
from app.services.mcp_server import TOOLS_BY_NAME, ApiResponse, MCPServer
from app.services.periods import lock_period
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    BusinessPartner,
    CashBook,
    CashClosing,
    CashEntry,
    Company,
    Document,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    Period,
    TaxCode,
    Tenant,
    User,
)

TODAY = business_today()
THREE_DAYS_AGO = TODAY - timedelta(days=3)
TWO_DAYS_AGO = TODAY - timedelta(days=2)
YESTERDAY = TODAY - timedelta(days=1)
TOMORROW = TODAY + timedelta(days=1)


@pytest.fixture(scope="module")
def migrated_template(tmp_path_factory) -> Path:
    """Einmal migrierte Datenbank; jeder Test arbeitet auf einer Kopie (mit Triggern)."""
    path = tmp_path_factory.mktemp("cash-book-template") / "template.db"
    create_session_factory(f"sqlite+pysqlite:///{path}")
    return path


def _database_url(tmp_path: Path, template: Path) -> str:
    target = tmp_path / "cash.db"
    shutil.copy(template, target)
    return f"sqlite+pysqlite:///{target}"


@pytest.fixture()
def session(tmp_path: Path, migrated_template: Path):
    session_factory = create_session_factory(_database_url(tmp_path, migrated_template))
    with session_factory() as db:
        yield db


class Seed:
    """Gesellschaft mit Kassen-, Gegen- und Steuerkonten."""

    def __init__(self, session: Session, name: str = "Kassen GmbH") -> None:
        tenant = Tenant(name=f"{name} Mandant")
        company = Company(tenant=tenant, name=name, currency_code="EUR")
        session.add_all([tenant, company])
        session.flush()
        self.tenant_id = tenant.id
        self.company_id = company.id
        self.accounts: dict[str, Account] = {}
        for code, account_name, account_type, subledger in (
            ("1000", "Kasse", "asset", None),
            ("1010", "Nebenkasse", "asset", None),
            ("1200", "Bank", "asset", None),
            ("1400", "Forderungen", "asset", "debtor"),
            ("1460", "Geldtransit", "asset", None),
            ("1576", "Vorsteuer 19 %", "asset", None),
            ("1776", "Umsatzsteuer 19 %", "liability", None),
            ("4900", "Sonstiger Aufwand", "expense", None),
            ("4990", "Kassendifferenzen", "expense", None),
            ("8400", "Erlöse", "revenue", None),
            ("9000", "Saldenvorträge", "equity", None),
        ):
            account = Account(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                name=account_name,
                account_type=account_type,
                subledger=subledger,
            )
            session.add(account)
            self.accounts[code] = account
        session.flush()
        self.vst = TaxCode(
            tenant_id=tenant.id,
            company_id=company.id,
            code="VST19",
            rate=Decimal("19.00"),
            kind="input",
            vat_account_id=self.accounts["1576"].id,
        )
        self.ust = TaxCode(
            tenant_id=tenant.id,
            company_id=company.id,
            code="UST19",
            rate=Decimal("19.00"),
            kind="output",
            vat_account_id=self.accounts["1776"].id,
        )
        session.add_all([self.vst, self.ust])
        session.commit()

    def account_id(self, code: str) -> int:
        return self.accounts[code].id


@pytest.fixture()
def seed(session: Session) -> Seed:
    return Seed(session)


def _book(
    session: Session,
    seed: Seed,
    *,
    opening: str | None = "500.00",
    opening_date: date = THREE_DAYS_AGO,
    name: str = "Hauptkasse",
    code: str = "1000",
) -> CashBook:
    return create_cash_book(
        session=session,
        company_id=seed.company_id,
        account_id=seed.account_id(code),
        name=name,
        opening_amount=opening,
        opening_date=opening_date,
        opening_contra_account_id=seed.account_id("9000") if opening else None,
        changed_by="pytest",
    )


def _entry(
    session: Session,
    seed: Seed,
    book: CashBook,
    *,
    entry_type: str = "receipt",
    amount: str = "100.00",
    entry_date: date = THREE_DAYS_AGO,
    contra: str = "8400",
    **extra,
) -> CashEntry:
    return create_cash_entry(
        session=session,
        cash_book_id=book.id,
        entry_type=entry_type,
        entry_date=entry_date,
        amount=amount,
        description=extra.pop("description", "Barverkauf"),
        contra_account_id=seed.account_id(contra),
        changed_by="pytest",
        **extra,
    )


def _lines(session: Session, journal_entry_id: int) -> list[tuple[str, Decimal, Decimal]]:
    rows = session.execute(
        select(Account.code, JournalEntryLine.debit_amount, JournalEntryLine.credit_amount)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .where(JournalEntryLine.journal_entry_id == journal_entry_id)
        .order_by(JournalEntryLine.line_number)
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def _count(session: Session, model) -> int:
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Kasse anlegen und Anfangsbestand
# ---------------------------------------------------------------------------


def test_opening_with_contra_account_is_booked_as_first_entry(session, seed) -> None:
    book = _book(session, seed)

    entry = session.execute(select(CashEntry)).scalar_one()
    assert (entry.entry_number, entry.entry_type, entry.direction) == (1, "opening", "in")
    assert entry.amount == Decimal("500.00")
    assert entry.balance_after == Decimal("500.00")
    assert entry.previous_hash == "0" * 64
    assert entry.content_hash == calculate_cash_entry_hash(entry)
    assert entry.contra_account_id == seed.account_id("9000")
    assert _lines(session, entry.journal_entry_id) == [
        ("1000", Decimal("500.00"), Decimal("0.00")),
        ("9000", Decimal("0.00"), Decimal("500.00")),
    ]
    assert session.get(JournalEntry, entry.journal_entry_id).source == "cash_book"
    assert book.last_entry_number == 1
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).account_matches


def _carry(session, seed, amount: str = "200.00", entry_date: date = THREE_DAYS_AGO) -> None:
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=seed.company_id,
            entry_date=entry_date,
            description="Saldo vor dem Kassenbuch",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(account_id=seed.account_id("1000"), debit_amount=Decimal(amount)),
                JournalLineInput(account_id=seed.account_id("9000"), credit_amount=Decimal(amount)),
            ],
        ),
    )


def test_opening_without_contra_takes_over_the_account_balance(session, seed) -> None:
    _carry(session, seed)
    book = create_cash_book(
        session=session,
        company_id=seed.company_id,
        account_id=seed.account_id("1000"),
        name="Hauptkasse",
        opening_date=THREE_DAYS_AGO,
        changed_by="pytest",
    )

    entry = session.execute(select(CashEntry)).scalar_one()
    assert entry.journal_entry_id is None
    assert entry.contra_account_id is None
    assert entry.amount == Decimal("200.00")
    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid and result.account_matches
    assert result.unlinked_postings == ()


def test_opening_without_contra_must_match_the_account_balance(session, seed) -> None:
    _carry(session, seed)
    with pytest.raises(CashBookError, match="muss der Anfangsbestand dem Kontensaldo"):
        create_cash_book(
            session=session,
            company_id=seed.company_id,
            account_id=seed.account_id("1000"),
            name="Hauptkasse",
            opening_amount="150.00",
            opening_date=THREE_DAYS_AGO,
            changed_by="pytest",
        )
    assert _count(session, CashBook) == 0


def test_opening_with_contra_is_refused_when_the_account_already_carries_a_balance(
    session, seed
) -> None:
    _carry(session, seed)
    with pytest.raises(CashBookError, match="doppelt gebucht"):
        _book(session, seed)
    assert _count(session, CashBook) == 0


def test_book_without_opening_starts_chain_with_first_entry(session, seed) -> None:
    book = create_cash_book(
        session=session,
        company_id=seed.company_id,
        account_id=seed.account_id("1000"),
        name="Hauptkasse",
        changed_by="pytest",
    )
    assert _count(session, CashEntry) == 0
    assert current_balance(session, book.id) == Decimal("0.00")

    entry = _entry(session, seed, book, amount="10.00", entry_date=TODAY)
    assert entry.entry_number == 1
    assert entry.previous_hash == "0" * 64


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("1776", "kein Aktivkonto"),
        ("1400", "Sammelkonto"),
    ],
)
def test_cash_account_must_be_a_plain_asset_account(session, seed, code, message) -> None:
    with pytest.raises(CashBookError, match=message):
        create_cash_book(
            session=session,
            company_id=seed.company_id,
            account_id=seed.account_id(code),
            name="Kasse",
            changed_by="pytest",
        )


def test_bank_account_and_second_book_on_same_account_are_refused(session, seed) -> None:
    session.add(
        BankTransaction(
            tenant_id=seed.tenant_id,
            company_id=seed.company_id,
            bank_account_id=seed.account_id("1200"),
            booking_date=TODAY,
            amount=Decimal("1.00"),
            purpose="Umsatz",
            dedup_hash="a" * 64,
        )
    )
    session.commit()
    with pytest.raises(CashBookError, match="Bankkonto"):
        create_cash_book(
            session=session,
            company_id=seed.company_id,
            account_id=seed.account_id("1200"),
            name="Bank als Kasse",
            changed_by="pytest",
        )

    _book(session, seed)
    with pytest.raises(CashBookError, match="gehört bereits"):
        _book(session, seed, name="Zweite Kasse")
    with pytest.raises(CashBookError, match="bereits eine Kasse mit dem Namen"):
        _book(session, seed, code="1010", opening=None)


def test_foreign_account_is_refused(session, seed) -> None:
    other = Seed(session, "Fremd GmbH")
    with pytest.raises(CashBookError, match="Konto nicht gefunden"):
        create_cash_book(
            session=session,
            company_id=seed.company_id,
            account_id=other.account_id("1000"),
            name="Fremdkasse",
            changed_by="pytest",
        )


# ---------------------------------------------------------------------------
# Einträge und Buchungen
# ---------------------------------------------------------------------------


def test_receipt_books_gross_with_output_tax(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book, amount="119.00", tax_code_id=seed.ust.id)

    assert entry.entry_number == 2
    assert entry.balance_after == Decimal("619.00")
    assert entry.direction == "in"
    assert _lines(session, entry.journal_entry_id) == [
        ("1000", Decimal("119.00"), Decimal("0.00")),
        ("8400", Decimal("0.00"), Decimal("100.00")),
        ("1776", Decimal("0.00"), Decimal("19.00")),
    ]


def test_payment_books_gross_with_input_tax_and_rounding_on_tax_line(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(
        session,
        seed,
        book,
        entry_type="payment",
        amount="1.22",
        contra="4900",
        tax_code_id=seed.vst.id,
    )

    assert entry.direction == "out"
    assert entry.balance_after == Decimal("498.78")
    # Wie die Bankbuchung: 1,22 € = 1,03 € + 0,19 €, das Brutto geht immer auf.
    assert _lines(session, entry.journal_entry_id) == [
        ("1000", Decimal("0.00"), Decimal("1.22")),
        ("4900", Decimal("1.03"), Decimal("0.00")),
        ("1576", Decimal("0.19"), Decimal("0.00")),
    ]


def test_tax_code_direction_is_checked_like_any_booking(session, seed) -> None:
    book = _book(session, seed)
    with pytest.raises(CashBookError, match="nicht zulässig"):
        _entry(session, seed, book, tax_code_id=seed.vst.id)
    assert _count(session, CashEntry) == 1


def test_entry_carries_partner_and_links_document(session, seed) -> None:
    book = _book(session, seed)
    partner = BusinessPartner(
        tenant_id=seed.tenant_id,
        company_id=seed.company_id,
        name="Lieferant Bar",
        creditor_number="70000",
    )
    document = Document(
        tenant_id=seed.tenant_id,
        company_id=seed.company_id,
        file_name="quittung.pdf",
        storage_key="k/quittung.pdf",
        mime_type="application/pdf",
    )
    session.add_all([partner, document])
    session.commit()

    entry = _entry(
        session,
        seed,
        book,
        entry_type="payment",
        amount="11.90",
        contra="4900",
        tax_code_id=seed.vst.id,
        partner_id=partner.id,
        document_id=document.id,
    )

    assert entry.partner_id == partner.id
    assert entry.document_id == document.id
    session.refresh(document)
    assert document.journal_entry_id == entry.journal_entry_id
    partners_by_code = dict(
        session.execute(
            select(Account.code, JournalEntryLine.partner_id)
            .join(Account, Account.id == JournalEntryLine.account_id)
            .where(JournalEntryLine.journal_entry_id == entry.journal_entry_id)
        ).all()
    )
    assert partners_by_code == {"1000": partner.id, "4900": partner.id, "1576": None}

    with pytest.raises(CashBookError, match="bereits mit einer Buchung"):
        _entry(session, seed, book, document_id=document.id)


def test_partner_role_is_checked_on_collective_accounts(session, seed) -> None:
    book = _book(session, seed)
    supplier = BusinessPartner(
        tenant_id=seed.tenant_id,
        company_id=seed.company_id,
        name="Nur Lieferant",
        creditor_number="70001",
    )
    session.add(supplier)
    session.commit()
    with pytest.raises(CashBookError):
        _entry(session, seed, book, contra="1400", partner_id=supplier.id)
    with pytest.raises(CashBookError, match="nicht gefunden"):
        _entry(session, seed, book, partner_id=999_999)
    assert _count(session, CashEntry) == 1


def test_replaced_document_version_is_refused(session, seed) -> None:
    book = _book(session, seed)
    document = Document(
        tenant_id=seed.tenant_id,
        company_id=seed.company_id,
        file_name="alt.pdf",
        storage_key="k/alt.pdf",
        mime_type="application/pdf",
        is_current=False,
    )
    session.add(document)
    session.commit()
    with pytest.raises(CashBookError, match="aktuelle Fassung"):
        _entry(session, seed, book, document_id=document.id)


def test_entry_date_rules(session, seed) -> None:
    book = _book(session, seed, opening_date=TWO_DAYS_AGO)

    with pytest.raises(CashBookError, match="Zukunft"):
        _entry(session, seed, book, entry_date=TOMORROW)
    with pytest.raises(CashBookError, match="chronologisch"):
        _entry(session, seed, book, entry_date=THREE_DAYS_AGO)
    assert _entry(session, seed, book, entry_date=TWO_DAYS_AGO).entry_number == 2


def test_balance_must_never_become_negative(session, seed) -> None:
    book = _book(session, seed, opening="50.00")

    with pytest.raises(CashBookError, match="Betriebsprüfung"):
        _entry(session, seed, book, entry_type="payment", amount="50.01", contra="4900")

    entry = _entry(session, seed, book, entry_type="payment", amount="50.00", contra="4900")
    assert entry.balance_after == Decimal("0.00")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"amount": "0"}, "größer als null"),
        ({"amount": "-5"}, "größer als null"),
        ({"amount": "1.234"}, "Nachkommastellen"),
        ({"amount": "abc"}, "Dezimalzahl"),
        ({"amount": "1000000000000"}, "zu groß"),
        ({"entry_type": "opening"}, "receipt"),
        ({"entry_type": "difference"}, "receipt"),
        ({"description": "  "}, "Beschreibung"),
        ({"description": "x" * 256}, "höchstens 255"),
        ({"contra": "1000"}, "Kassenkonto selbst"),
    ],
)
def test_entry_input_validation(session, seed, overrides, message) -> None:
    book = _book(session, seed)
    with pytest.raises(CashBookError, match=message):
        _entry(session, seed, book, **overrides)
    assert _count(session, CashEntry) == 1


def test_inactive_book_takes_no_entries_and_needs_zero_balance(session, seed) -> None:
    book = _book(session, seed, opening="50.00")

    with pytest.raises(CashBookError, match="Bestand von"):
        update_cash_book(session=session, cash_book_id=book.id, is_active=False, changed_by="t")

    _entry(session, seed, book, entry_type="payment", amount="50.00", contra="1460")
    update_cash_book(session=session, cash_book_id=book.id, is_active=False, changed_by="t")
    with pytest.raises(CashBookError, match="inaktiv"):
        _entry(session, seed, book)
    with pytest.raises(CashBookError, match="inaktiv"):
        create_cash_closing(
            session=session,
            cash_book_id=book.id,
            closing_date=TODAY,
            counted_balance="0",
            changed_by="t",
        )

    update_cash_book(session=session, cash_book_id=book.id, is_active=True, changed_by="t")
    assert _entry(session, seed, book, entry_date=TODAY).entry_number == 3


def test_update_is_audited_with_before_and_after(session, seed) -> None:
    book = _book(session, seed)
    update_cash_book(session=session, cash_book_id=book.id, name="Ladenkasse", changed_by="t")
    event = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "cash_book", AuditLog.action == "updated")
    ).scalar_one()
    assert event.payload["before"]["name"] == "Hauptkasse"
    assert event.payload["after"]["name"] == "Ladenkasse"


def test_failed_entry_rolls_back_everything_and_leaves_no_number_gap(session, seed) -> None:
    book = _book(session, seed)
    period = session.execute(
        select(Period).where(
            Period.start_date <= THREE_DAYS_AGO,
            Period.end_date >= THREE_DAYS_AGO,
            Period.is_closing.is_(False),
        )
    ).scalar_one()
    lock_period(session=session, period_id=period.id, locked_by="pytest")
    journal_entries = _count(session, JournalEntry)
    audit_events = _count(session, AuditLog)

    with pytest.raises(CashBookError, match="gesperrt"):
        _entry(session, seed, book)

    assert _count(session, CashEntry) == 1
    assert _count(session, JournalEntry) == journal_entries
    assert _count(session, AuditLog) == audit_events
    session.refresh(book)
    assert book.last_entry_number == 1

    # Nach dem gesperrten Versuch geht es ohne Lücke weiter.
    assert _entry(session, seed, book, entry_date=TODAY).entry_number == 2


def test_closed_fiscal_year_blocks_the_entry(session, seed) -> None:
    book = _book(session, seed)
    fiscal_year = session.execute(select(FiscalYear)).scalars().first()
    fiscal_year.is_closed = True
    session.commit()
    with pytest.raises(CashBookError, match="abgeschlossen"):
        _entry(session, seed, book)
    assert _count(session, CashEntry) == 1


def test_every_entry_appends_to_the_hash_chain_and_is_audited(session, seed) -> None:
    book = _book(session, seed)
    second = _entry(session, seed, book)
    third = _entry(session, seed, book, entry_type="payment", amount="20.00", contra="4900")

    first = session.execute(select(CashEntry).where(CashEntry.entry_number == 1)).scalar_one()
    assert second.previous_hash == first.content_hash
    assert third.previous_hash == second.content_hash
    assert len({first.content_hash, second.content_hash, third.content_hash}) == 3
    events = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "cash_entry")
    ).scalars().all()
    assert [event.payload["entry_number"] for event in events] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Unveränderbarkeit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE cash_entry SET description = 'geändert'",
        "UPDATE cash_entry SET amount = 1",
        "DELETE FROM cash_entry",
        "UPDATE cash_closing SET counted_balance = 1",
        "DELETE FROM cash_closing",
    ],
)
def test_entries_and_closings_are_immutable_in_the_database(session, seed, statement) -> None:
    book = _book(session, seed)
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="500.00",
        changed_by="pytest",
    )
    with pytest.raises(IntegrityError, match="immutable"):
        session.execute(text(statement))
    session.rollback()


# ---------------------------------------------------------------------------
# Storno
# ---------------------------------------------------------------------------


def test_reversal_mirrors_entry_at_current_date_and_reverses_booking(session, seed) -> None:
    book = _book(session, seed)
    original = _entry(session, seed, book, amount="119.00", tax_code_id=seed.ust.id)

    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=original.id, changed_by="pytest"
    )

    assert reversal.entry_type == "reversal"
    assert reversal.direction == "out"
    assert reversal.entry_date == TODAY
    assert reversal.amount == original.amount
    assert reversal.balance_after == Decimal("500.00")
    assert reversal.reversal_of_id == original.id
    assert reversal.description.startswith(f"Storno Nr. {original.entry_number}:")
    assert reversal.contra_account_id == original.contra_account_id
    assert reversal.tax_code_id == original.tax_code_id
    journal_entry = session.get(JournalEntry, reversal.journal_entry_id)
    assert journal_entry.reversal_of_id == original.journal_entry_id
    assert journal_entry.is_finalized is True
    assert _lines(session, reversal.journal_entry_id) == [
        ("1000", Decimal("0.00"), Decimal("119.00")),
        ("8400", Decimal("100.00"), Decimal("0.00")),
        ("1776", Decimal("19.00"), Decimal("0.00")),
    ]
    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid and result.account_matches


def test_entry_can_be_reversed_only_once_and_reversals_not_at_all(session, seed) -> None:
    book = _book(session, seed)
    original = _entry(session, seed, book)
    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=original.id, changed_by="pytest"
    )

    with pytest.raises(CashBookError, match="bereits storniert"):
        reverse_cash_entry(
            session=session, cash_book_id=book.id, entry_id=original.id, changed_by="pytest"
        )
    with pytest.raises(CashBookError, match="selbst ein Storno"):
        reverse_cash_entry(
            session=session, cash_book_id=book.id, entry_id=reversal.id, changed_by="pytest"
        )
    assert session.execute(
        select(func.count(CashEntry.id)).where(CashEntry.entry_type == "reversal")
    ).scalar_one() == 1


def test_reversal_of_receipt_must_not_make_the_balance_negative(session, seed) -> None:
    book = _book(session, seed, opening=None)
    receipt = _entry(session, seed, book, amount="100.00")
    _entry(session, seed, book, entry_type="payment", amount="80.00", contra="4900")
    reversals = _count(session, JournalEntry)

    with pytest.raises(CashBookError, match="Betriebsprüfung"):
        reverse_cash_entry(
            session=session, cash_book_id=book.id, entry_id=receipt.id, changed_by="pytest"
        )
    assert _count(session, JournalEntry) == reversals


def test_reversal_releases_the_document_for_the_corrected_entry(session, seed) -> None:
    book = _book(session, seed)
    document = Document(
        tenant_id=seed.tenant_id,
        company_id=seed.company_id,
        file_name="quittung.pdf",
        storage_key="k/quittung.pdf",
        mime_type="application/pdf",
    )
    session.add(document)
    session.commit()
    wrong = _entry(session, seed, book, amount="10.00", document_id=document.id)
    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=wrong.id, changed_by="pytest"
    )
    session.refresh(document)
    assert document.journal_entry_id is None
    assert reversal.document_id == document.id
    assert wrong.document_id == document.id

    corrected = _entry(
        session, seed, book, amount="12.00", entry_date=TODAY, document_id=document.id
    )
    session.refresh(document)
    assert document.journal_entry_id == corrected.journal_entry_id
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).valid


def test_reversal_of_payment_raises_the_balance(session, seed) -> None:
    book = _book(session, seed)
    payment = _entry(session, seed, book, entry_type="payment", amount="80.00", contra="4900")
    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=payment.id, changed_by="pytest"
    )
    assert reversal.direction == "in"
    assert reversal.balance_after == Decimal("500.00")


def test_opening_without_booking_cannot_be_reversed(session, seed) -> None:
    _carry(session, seed)
    book = create_cash_book(
        session=session,
        company_id=seed.company_id,
        account_id=seed.account_id("1000"),
        name="Hauptkasse",
        opening_date=THREE_DAYS_AGO,
        changed_by="pytest",
    )
    entry = session.execute(select(CashEntry)).scalar_one()
    with pytest.raises(CashBookError, match="ohne Buchung"):
        reverse_cash_entry(
            session=session, cash_book_id=book.id, entry_id=entry.id, changed_by="pytest"
        )


def test_entry_of_another_book_is_not_found(session, seed) -> None:
    first = _book(session, seed)
    second = _book(session, seed, name="Nebenkasse", code="1010", opening=None)
    entry = _entry(session, seed, first)
    with pytest.raises(CashBookError, match="nicht gefunden"):
        reverse_cash_entry(
            session=session, cash_book_id=second.id, entry_id=entry.id, changed_by="pytest"
        )


def test_booking_of_a_cash_entry_can_only_be_reversed_in_the_cash_book(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book)
    with pytest.raises(JournalEntryCreationError, match="Kassenbuch"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=entry.journal_entry_id,
            reversal_date=TODAY,
            changed_by="pytest",
        )
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).account_matches


def test_open_booking_can_be_amended_without_touching_the_cash_book(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book)
    amend_journal_entry(
        session=session,
        company_id=seed.company_id,
        journal_entry_id=entry.journal_entry_id,
        service_date=YESTERDAY,
        changed_by="pytest",
    )
    assert session.get(JournalEntry, entry.journal_entry_id).service_date == YESTERDAY
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).valid


def test_finalized_booking_can_still_be_reversed_through_the_cash_book(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book)
    finalize_journal_entries_until(
        session=session, company_id=seed.company_id, up_to_date=TODAY, changed_by="pytest"
    )
    assert session.get(JournalEntry, entry.journal_entry_id).is_finalized is True

    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=entry.id, changed_by="pytest"
    )
    assert reversal.balance_after == Decimal("500.00")
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).valid


# ---------------------------------------------------------------------------
# Kassensturz
# ---------------------------------------------------------------------------


def test_closing_without_difference_locks_the_days(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book)

    closing = create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TWO_DAYS_AGO,
        counted_balance="600.00",
        changed_by="pytest",
    )
    assert closing.expected_balance == Decimal("600.00")
    assert closing.difference == Decimal("0.00")
    assert closing.difference_entry_id is None
    assert _count(session, CashEntry) == 2

    with pytest.raises(CashBookError, match="abgeschlossen"):
        _entry(session, seed, book, entry_date=TWO_DAYS_AGO)
    assert _entry(session, seed, book, entry_date=YESTERDAY).entry_number == 3
    with pytest.raises(CashBookError, match="bereits durch den Kassensturz"):
        create_cash_closing(
            session=session,
            cash_book_id=book.id,
            closing_date=TWO_DAYS_AGO,
            counted_balance="600.00",
            changed_by="pytest",
        )


def test_closing_books_a_shortage_as_difference_entry(session, seed) -> None:
    book = _book(session, seed)

    closing = create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="495,50".replace(",", "."),
        difference_account_id=seed.account_id("4990"),
        changed_by="pytest",
    )

    assert closing.expected_balance == Decimal("500.00")
    assert closing.counted_balance == Decimal("495.50")
    assert closing.difference == Decimal("-4.50")
    entry = session.get(CashEntry, closing.difference_entry_id)
    assert (entry.entry_type, entry.direction, entry.amount) == (
        "difference",
        "out",
        Decimal("4.50"),
    )
    assert entry.entry_date == TODAY
    assert entry.balance_after == Decimal("495.50")
    assert _lines(session, entry.journal_entry_id) == [
        ("1000", Decimal("0.00"), Decimal("4.50")),
        ("4990", Decimal("4.50"), Decimal("0.00")),
    ]
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).valid


def test_closing_books_a_surplus_as_difference_entry(session, seed) -> None:
    book = _book(session, seed)
    closing = create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="510.00",
        difference_account_id=seed.account_id("4990"),
        description="Wechselgeld falsch herausgegeben",
        changed_by="pytest",
    )
    entry = session.get(CashEntry, closing.difference_entry_id)
    assert (entry.direction, entry.amount) == ("in", Decimal("10.00"))
    assert entry.description == "Wechselgeld falsch herausgegeben"
    assert entry.balance_after == Decimal("510.00")
    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid and result.account_matches


def test_difference_needs_an_account(session, seed) -> None:
    book = _book(session, seed)
    with pytest.raises(CashBookError, match="braucht ein Konto"):
        create_cash_closing(
            session=session,
            cash_book_id=book.id,
            closing_date=TODAY,
            counted_balance="499.00",
            changed_by="pytest",
        )
    assert _count(session, CashClosing) == 0


@pytest.mark.parametrize(
    ("closing_date", "counted", "message"),
    [
        (TOMORROW, "500.00", "Zukunft"),
        (TWO_DAYS_AGO, "-1.00", "negativ"),
        (None, "500.00", "Datum"),
        (TODAY, "abc", "Dezimalzahl"),
    ],
)
def test_closing_input_validation(session, seed, closing_date, counted, message) -> None:
    book = _book(session, seed)
    with pytest.raises(CashBookError, match=message):
        create_cash_closing(
            session=session,
            cash_book_id=book.id,
            closing_date=closing_date,
            counted_balance=counted,
            changed_by="pytest",
        )


def test_closing_must_not_lie_before_the_last_entry(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book, entry_date=YESTERDAY)
    with pytest.raises(CashBookError, match="bereits Einträge"):
        create_cash_closing(
            session=session,
            cash_book_id=book.id,
            closing_date=TWO_DAYS_AGO,
            counted_balance="600.00",
            changed_by="pytest",
        )


def test_reversal_is_refused_when_today_is_closed_and_rolls_back(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book)
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="600.00",
        changed_by="pytest",
    )
    journal_entries = _count(session, JournalEntry)

    with pytest.raises(CashBookError, match="abgeschlossen"):
        reverse_cash_entry(
            session=session, cash_book_id=book.id, entry_id=entry.id, changed_by="pytest"
        )
    assert _count(session, JournalEntry) == journal_entries


def test_reversal_after_a_closing_takes_the_current_date(session, seed) -> None:
    book = _book(session, seed)
    entry = _entry(session, seed, book)
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=YESTERDAY,
        counted_balance="600.00",
        changed_by="pytest",
    )
    reversal = reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=entry.id, changed_by="pytest"
    )
    assert reversal.entry_date == TODAY
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).valid


# ---------------------------------------------------------------------------
# Kassenbericht
# ---------------------------------------------------------------------------


def _report_fixture(session, seed) -> CashBook:
    book = _book(session, seed, opening_date=THREE_DAYS_AGO)
    _entry(session, seed, book, amount="100.00", entry_date=THREE_DAYS_AGO)
    _entry(
        session,
        seed,
        book,
        entry_type="payment",
        amount="30.00",
        entry_date=TWO_DAYS_AGO,
        contra="4900",
    )
    _entry(session, seed, book, amount="20.00", entry_date=TWO_DAYS_AGO)
    _entry(session, seed, book, amount="5.00", entry_date=YESTERDAY)
    return book


def test_report_with_opening_entry_in_range(session, seed) -> None:
    book = _report_fixture(session, seed)
    report = cash_report(session=session, cash_book_id=book.id)

    assert report.opening_balance == Decimal("500.00")
    assert report.total_receipts == Decimal("125.00")
    assert report.total_payments == Decimal("30.00")
    assert report.closing_balance == Decimal("595.00")
    assert [
        (day.day, day.receipts, day.payments, day.balance) for day in report.days
    ] == [
        (THREE_DAYS_AGO, Decimal("100.00"), Decimal("0.00"), Decimal("600.00")),
        (TWO_DAYS_AGO, Decimal("20.00"), Decimal("30.00"), Decimal("590.00")),
        (YESTERDAY, Decimal("5.00"), Decimal("0.00"), Decimal("595.00")),
    ]
    assert [entry.entry_number for entry in report.entries] == [1, 2, 3, 4, 5]


def test_report_for_a_later_period_starts_with_the_previous_balance(session, seed) -> None:
    book = _report_fixture(session, seed)
    report = cash_report(
        session=session, cash_book_id=book.id, date_from=TWO_DAYS_AGO, date_to=TWO_DAYS_AGO
    )
    assert report.opening_balance == Decimal("600.00")
    assert report.total_receipts == Decimal("20.00")
    assert report.total_payments == Decimal("30.00")
    assert report.closing_balance == Decimal("590.00")
    assert [entry.entry_number for entry in report.entries] == [3, 4]


def test_report_rejects_inverted_range_and_handles_empty_period(session, seed) -> None:
    book = _report_fixture(session, seed)
    with pytest.raises(CashBookError, match="Von-Datum"):
        cash_report(session=session, cash_book_id=book.id, date_from=TODAY, date_to=YESTERDAY)
    report = cash_report(session=session, cash_book_id=book.id, date_from=TODAY, date_to=TODAY)
    assert report.opening_balance == report.closing_balance == Decimal("595.00")
    assert report.entries == ()


def test_report_csv_contains_balances_entries_and_daily_rows(session, seed) -> None:
    book = _report_fixture(session, seed)
    entry = session.execute(
        select(CashEntry).where(CashEntry.entry_number == 2)
    ).scalar_one()
    reverse_cash_entry(
        session=session, cash_book_id=book.id, entry_id=entry.id, changed_by="pytest"
    )
    report = cash_report(session=session, cash_book_id=book.id, date_to=TODAY)

    rows = list(csv.DictReader(io.StringIO(cash_report_csv(report))))
    assert rows[0]["Satzart"] == "Anfangsbestand"
    assert rows[0]["Bestand"] == "500.00"
    assert rows[-1]["Satzart"] == "Endbestand"
    assert rows[-1]["Bestand"] == "495.00"
    assert rows[-1]["Einnahme"] == "125.00"
    assert rows[-1]["Ausgabe"] == "130.00"
    entries = [row for row in rows if row["Satzart"] == "Eintrag"]
    assert [row["Nr"] for row in entries] == ["1", "2", "3", "4", "5", "6"]
    assert entries[0]["Art"] == "Anfangsbestand"
    assert entries[0]["Einnahme"] == ""
    assert entries[1]["Storniert durch"] == "6"
    assert entries[5]["Storno von"] == "2"
    assert entries[5]["Ausgabe"] == "100.00"
    daily = [row for row in rows if row["Satzart"] == "Tagessaldo"]
    assert [row["Datum"] for row in daily] == [
        THREE_DAYS_AGO.isoformat(),
        TWO_DAYS_AGO.isoformat(),
        YESTERDAY.isoformat(),
        TODAY.isoformat(),
    ]
    assert daily[1]["Bestand"] == "590.00"


# ---------------------------------------------------------------------------
# Prüfung
# ---------------------------------------------------------------------------


def _drop_triggers(session: Session) -> None:
    """Simuliert eine Manipulation an der Datenbank vorbei."""
    for table in ("cash_entry", "cash_closing"):
        for suffix in ("no_update", "no_delete"):
            session.execute(text(f"DROP TRIGGER obk_{table}_{suffix}"))
    session.commit()


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_integrity_of_a_healthy_book(session, seed) -> None:
    book = _report_fixture(session, seed)
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=YESTERDAY,
        counted_balance="590.00",
        difference_account_id=seed.account_id("4990"),
        changed_by="pytest",
    )
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="590.00",
        changed_by="pytest",
    )
    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid is True
    assert result.issues == ()
    assert result.checked_entries == 6
    assert result.checked_closings == 2
    assert result.account_matches is True
    assert result.book_balance == result.account_balance == Decimal("590.00")
    assert len(result.head_hash) == 64


def test_posting_past_the_cash_book_is_reported_but_does_not_block(session, seed) -> None:
    book = _book(session, seed)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=seed.company_id,
            entry_date=TODAY,
            description="Manuelle Kassenbuchung",
            status="posted",
            changed_by="pytest",
            lines=[
                JournalLineInput(account_id=seed.account_id("1000"), debit_amount=Decimal("7.00")),
                JournalLineInput(account_id=seed.account_id("8400"), credit_amount=Decimal("7.00")),
            ],
        ),
    )

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid is True
    assert result.account_matches is False
    assert result.account_balance - result.book_balance == Decimal("7.00")
    assert [issue.severity for issue in result.issues] == ["warning"]
    assert [item["description"] for item in result.unlinked_postings] == [
        "Manuelle Kassenbuchung"
    ]
    assert result.as_dict()["account_difference"] == "7.00"

    # Das Kassenbuch bleibt benutzbar.
    assert _entry(session, seed, book, entry_date=TODAY).entry_number == 2


def test_carryforward_does_not_count_towards_the_account_balance(session, seed) -> None:
    book = _book(session, seed)
    create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=seed.company_id,
            entry_date=TODAY,
            description="Saldovortrag",
            status="posted",
            changed_by="pytest",
            source="carryforward",
            lines=[
                JournalLineInput(
                    account_id=seed.account_id("1000"), debit_amount=Decimal("500.00")
                ),
                JournalLineInput(
                    account_id=seed.account_id("9000"), credit_amount=Decimal("500.00")
                ),
            ],
        ),
    )
    assert account_balance(session, seed.account_id("1000")) == Decimal("500.00")
    assert verify_cash_book_integrity(session=session, cash_book_id=book.id).account_matches


def test_integrity_detects_changed_content(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book)
    _drop_triggers(session)
    session.execute(
        text("UPDATE cash_entry SET description = 'Manipuliert' WHERE entry_number = 2")
    )
    session.commit()
    session.expire_all()

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid is False
    assert _codes(result) == {"content_hash_mismatch"}


def test_integrity_detects_changed_amount(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book)
    _drop_triggers(session)
    session.execute(text("UPDATE cash_entry SET amount = 99 WHERE entry_number = 2"))
    session.commit()
    session.expire_all()

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert {"content_hash_mismatch", "balance_mismatch", "journal_entry_mismatch"} <= _codes(result)


def test_integrity_detects_removed_entry_in_the_middle(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book)
    _entry(session, seed, book)
    _drop_triggers(session)
    session.execute(text("DELETE FROM cash_entry WHERE entry_number = 2"))
    session.commit()
    session.expire_all()

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid is False
    assert {"number_gap", "previous_hash_mismatch", "balance_mismatch"} <= _codes(result)


def test_integrity_detects_removed_tail(session, seed) -> None:
    book = _book(session, seed)
    _entry(session, seed, book)
    _drop_triggers(session)
    session.execute(text("DELETE FROM cash_entry WHERE entry_number = 2"))
    session.commit()
    session.expire_all()

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert result.valid is False
    assert "number_counter_mismatch" in _codes(result)
    # Die Buchung der gelöschten Zeile steht nun am Kassenbuch vorbei.
    assert result.account_matches is False
    assert len(result.unlinked_postings) == 1


def test_integrity_detects_changed_closing(session, seed) -> None:
    book = _book(session, seed)
    create_cash_closing(
        session=session,
        cash_book_id=book.id,
        closing_date=TODAY,
        counted_balance="500.00",
        changed_by="pytest",
    )
    _drop_triggers(session)
    session.execute(text("UPDATE cash_closing SET expected_balance = 400, difference = 50"))
    session.commit()
    session.expire_all()

    result = verify_cash_book_integrity(session=session, cash_book_id=book.id)
    assert {
        "closing_expected_mismatch",
        "closing_difference_mismatch",
        "closing_entry_mismatch",
    } <= _codes(result)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_roundtrip(tmp_path: Path, migrated_template: Path) -> None:
    url = _database_url(tmp_path, migrated_template)
    # Mit Daten, auch Selbstbezug (Storno) und Verweis des Abschlusses auf einen Eintrag.
    with create_session_factory(url)() as db:
        seed = Seed(db)
        book = _book(db, seed)
        entry = _entry(db, seed, book)
        reverse_cash_entry(session=db, cash_book_id=book.id, entry_id=entry.id, changed_by="t")
        create_cash_closing(
            session=db,
            cash_book_id=book.id,
            closing_date=TODAY,
            counted_balance="490.00",
            difference_account_id=seed.account_id("4990"),
            changed_by="t",
        )
    engine = create_engine(url)
    config = _alembic_config(engine)

    command.downgrade(config, "-1")
    tables = set(inspect(engine).get_table_names())
    assert not {"cash_book", "cash_entry", "cash_closing"} & tables
    with engine.connect() as connection:
        assert not connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'obk_cash%'")
        ).all()

    command.upgrade(config, "head")
    assert {"cash_book", "cash_entry", "cash_closing"} <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND name LIKE 'obk_cash%'"
                )
            )
        }
    assert triggers == {
        "obk_cash_entry_no_update",
        "obk_cash_entry_no_delete",
        "obk_cash_closing_no_update",
        "obk_cash_closing_no_delete",
    }


# ---------------------------------------------------------------------------
# REST-API
# ---------------------------------------------------------------------------


WRITER = {"Authorization": "Bearer obk_kasse-schreibt"}
READER = {"Authorization": "Bearer obk_kasse-liest"}
FOREIGN = {"Authorization": "Bearer obk_kasse-fremd"}


@pytest.fixture()
def api(tmp_path: Path, migrated_template: Path):
    app = create_app({"TESTING": True, "DATABASE_URL": _database_url(tmp_path, migrated_template)})
    with app.extensions["db_session_factory"]() as db:
        own = Seed(db)
        other = Seed(db, "Fremd GmbH")
        for username, role, token, tenant_id in (
            ("kasse-schreibt", "Buchhalter", "obk_kasse-schreibt", own.tenant_id),
            ("kasse-liest", "Pruefer", "obk_kasse-liest", own.tenant_id),
            ("kasse-fremd", "Buchhalter", "obk_kasse-fremd", other.tenant_id),
        ):
            db.add(
                User(
                    username=username,
                    password_hash=hash_password("passwort"),
                    role=role,
                    tenant_id=tenant_id,
                    api_token_hash=hash_api_token(token),
                    api_token_last4=token[-4:],
                )
            )
        db.commit()
        account_ids = {code: account.id for code, account in own.accounts.items()}
        tax_ids = {"vst": own.vst.id, "ust": own.ust.id}
        company_id = own.company_id
    return app.test_client(), company_id, account_ids, tax_ids, app


def _api_book(client, company_id, accounts, opening: str = "100.00") -> dict:
    response = client.post(
        "/api/v1/cash-books",
        headers=WRITER,
        json={
            "company_id": company_id,
            "account_id": accounts["1000"],
            "name": "Hauptkasse",
            "opening_amount": opening,
            "opening_date": THREE_DAYS_AGO.isoformat(),
            "opening_contra_account_id": accounts["9000"],
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_api_full_flow(api) -> None:
    client, company_id, accounts, tax_ids, _ = api
    book = _api_book(client, company_id, accounts)
    assert book["balance"] == "100.00"
    assert book["account_code"] == "1000"
    assert book["entry_count"] == 1
    book_id = book["id"]

    created = client.post(
        f"/api/v1/cash-books/{book_id}/entries",
        headers=WRITER,
        json={
            "entry_type": "payment",
            "entry_date": TWO_DAYS_AGO.isoformat(),
            "amount": "11.90",
            "description": "Porto",
            "contra_account_id": accounts["4900"],
            "tax_code_id": tax_ids["vst"],
        },
    )
    assert created.status_code == 201, created.get_json()
    entry = created.get_json()
    assert entry["entry_number"] == 2
    assert entry["balance_after"] == "88.10"
    assert entry["posting_number"]
    assert entry["tax_code"] == "VST19"

    listing = client.get(f"/api/v1/cash-books/{book_id}/entries", headers=READER).get_json()
    assert listing["total"] == 2
    assert [item["entry_number"] for item in listing["entries"]] == [2, 1]

    reversal = client.post(
        f"/api/v1/cash-books/{book_id}/entries/{entry['id']}/reverse", headers=WRITER
    )
    assert reversal.status_code == 201
    assert reversal.get_json()["balance_after"] == "100.00"
    assert reversal.get_json()["reversal_of_number"] == 2
    again = client.post(
        f"/api/v1/cash-books/{book_id}/entries/{entry['id']}/reverse", headers=WRITER
    )
    assert again.status_code == 422
    listing = client.get(f"/api/v1/cash-books/{book_id}/entries", headers=READER).get_json()
    assert listing["entries"][1]["reversed_by_number"] == 3

    closing = client.post(
        f"/api/v1/cash-books/{book_id}/closings",
        headers=WRITER,
        json={
            "closing_date": TODAY.isoformat(),
            "counted_balance": "98.00",
            "difference_account_id": accounts["4990"],
        },
    )
    assert closing.status_code == 201, closing.get_json()
    assert closing.get_json()["difference"] == "-2.00"
    closings = client.get(f"/api/v1/cash-books/{book_id}/closings", headers=READER).get_json()
    assert closings["total"] == 1

    report = client.get(
        f"/api/v1/cash-books/{book_id}/report",
        headers=READER,
        query_string={"date_to": TODAY.isoformat()},
    ).get_json()
    assert report["opening_balance"] == "100.00"
    assert report["closing_balance"] == "98.00"
    assert len(report["entries"]) == 4
    assert len(report["closings"]) == 1

    exported = client.get(f"/api/v1/cash-books/{book_id}/report.csv", headers=READER)
    assert exported.status_code == 200
    assert exported.mimetype == "text/csv"
    assert "kassenbericht" in exported.headers["Content-Disposition"]
    assert exported.get_data(as_text=True).splitlines()[0].startswith("Satzart,Nr,Datum")
    linked = client.get(
        f"/api/v1/cash-books/{book_id}/report.csv",
        headers=READER,
        query_string={"download_link": 1},
    )
    assert linked.status_code == 200
    assert "download_url" in linked.get_json()

    integrity = client.get(f"/api/v1/cash-books/{book_id}/integrity", headers=READER).get_json()
    assert integrity["valid"] is True
    assert integrity["checked_entries"] == 4
    assert integrity["account_matches"] is True

    books = client.get(
        "/api/v1/cash-books", headers=READER, query_string={"company_id": company_id}
    ).get_json()
    assert [item["name"] for item in books["cash_books"]] == ["Hauptkasse"]
    assert books["cash_books"][0]["last_closing_date"] == TODAY.isoformat()
    assert client.get(f"/api/v1/cash-books/{book_id}", headers=READER).status_code == 200


def test_api_update_book(api) -> None:
    client, company_id, accounts, _, _ = api
    book = _api_book(client, company_id, accounts)
    renamed = client.patch(
        f"/api/v1/cash-books/{book['id']}", headers=WRITER, json={"name": "Ladenkasse"}
    )
    assert renamed.status_code == 200
    assert renamed.get_json()["name"] == "Ladenkasse"

    refused = client.patch(
        f"/api/v1/cash-books/{book['id']}", headers=WRITER, json={"is_active": False}
    )
    assert refused.status_code == 422
    assert "Bestand" in refused.get_json()["error"]

    client.post(
        f"/api/v1/cash-books/{book['id']}/entries",
        headers=WRITER,
        json={
            "entry_type": "payment",
            "entry_date": THREE_DAYS_AGO.isoformat(),
            "amount": "100.00",
            "description": "Einzahlung Bank",
            "contra_account_id": accounts["1460"],
        },
    )
    deactivated = client.patch(
        f"/api/v1/cash-books/{book['id']}", headers=WRITER, json={"is_active": False}
    )
    assert deactivated.status_code == 200
    visible = client.get(
        "/api/v1/cash-books", headers=WRITER, query_string={"company_id": company_id}
    ).get_json()
    assert visible["cash_books"] == []
    with_inactive = client.get(
        "/api/v1/cash-books",
        headers=WRITER,
        query_string={"company_id": company_id, "include_inactive": 1},
    ).get_json()
    assert len(with_inactive["cash_books"]) == 1


def test_api_validation_errors(api) -> None:
    client, company_id, accounts, _, _ = api
    assert client.get("/api/v1/cash-books", headers=WRITER).status_code == 400
    assert client.post("/api/v1/cash-books", headers=WRITER, json={}).status_code == 400
    assert client.post(
        "/api/v1/cash-books", headers=WRITER, json={"company_id": company_id, "foo": 1}
    ).status_code == 400
    assert client.post(
        "/api/v1/cash-books",
        headers=WRITER,
        json={"company_id": company_id, "account_id": "abc", "name": "x"},
    ).status_code == 400
    assert client.post(
        "/api/v1/cash-books",
        headers=WRITER,
        json={"company_id": company_id, "account_id": accounts["1776"], "name": "x"},
    ).status_code == 422

    book = _api_book(client, company_id, accounts)
    base = {
        "entry_type": "receipt",
        "entry_date": TODAY.isoformat(),
        "amount": "1.00",
        "description": "Test",
        "contra_account_id": accounts["8400"],
    }
    url = f"/api/v1/cash-books/{book['id']}/entries"
    assert client.post(url, headers=WRITER, json={**base, "unknown": 1}).status_code == 400
    assert client.post(url, headers=WRITER, json={**base, "entry_date": "x"}).status_code == 400
    assert client.post(url, headers=WRITER, json={**base, "tax_code_id": "x"}).status_code == 400
    assert client.post(url, headers=WRITER, json={**base, "amount": "-1"}).status_code == 422
    assert client.post(url, headers=WRITER, json={**base, "amount": None}).status_code == 422
    assert client.post(url, headers=WRITER, json={**base, "entry_type": "x"}).status_code == 422
    future = (TODAY + timedelta(days=1)).isoformat()
    assert client.post(url, headers=WRITER, json={**base, "entry_date": future}).status_code == 422
    assert client.post(url, headers=WRITER, json=base).status_code == 201

    assert client.get(
        f"/api/v1/cash-books/{book['id']}/report",
        headers=WRITER,
        query_string={"date_from": "kaputt"},
    ).status_code == 400
    assert client.get(
        f"/api/v1/cash-books/{book['id']}/report",
        headers=WRITER,
        query_string={"date_from": TODAY.isoformat(), "date_to": YESTERDAY.isoformat()},
    ).status_code == 422
    assert client.post(
        f"/api/v1/cash-books/{book['id']}/closings",
        headers=WRITER,
        json={"closing_date": "x", "counted_balance": "1"},
    ).status_code == 400
    assert client.get("/api/v1/cash-books/99999", headers=WRITER).status_code == 404


def test_api_auditors_read_but_do_not_write(api) -> None:
    client, company_id, accounts, _, _ = api
    book = _api_book(client, company_id, accounts)
    book_id = book["id"]
    first_entry = client.get(
        f"/api/v1/cash-books/{book_id}/entries", headers=READER
    ).get_json()["entries"][0]

    for method, url, body in (
        ("post", "/api/v1/cash-books", {"company_id": company_id}),
        ("patch", f"/api/v1/cash-books/{book_id}", {"name": "x"}),
        ("post", f"/api/v1/cash-books/{book_id}/entries", {}),
        ("post", f"/api/v1/cash-books/{book_id}/entries/{first_entry['id']}/reverse", None),
        ("post", f"/api/v1/cash-books/{book_id}/closings", {}),
    ):
        response = getattr(client, method)(url, headers=READER, json=body)
        assert response.status_code == 403, url

    for url in (
        f"/api/v1/cash-books/{book_id}",
        f"/api/v1/cash-books/{book_id}/entries",
        f"/api/v1/cash-books/{book_id}/closings",
        f"/api/v1/cash-books/{book_id}/report",
        f"/api/v1/cash-books/{book_id}/report.csv",
        f"/api/v1/cash-books/{book_id}/integrity",
    ):
        assert client.get(url, headers=READER).status_code == 200, url


def test_api_keeps_tenants_apart(api) -> None:
    client, company_id, accounts, _, _ = api
    book = _api_book(client, company_id, accounts)
    book_id = book["id"]
    first_entry = client.get(
        f"/api/v1/cash-books/{book_id}/entries", headers=WRITER
    ).get_json()["entries"][0]

    assert client.get(
        "/api/v1/cash-books", headers=FOREIGN, query_string={"company_id": company_id}
    ).status_code == 404
    assert client.post(
        "/api/v1/cash-books",
        headers=FOREIGN,
        json={"company_id": company_id, "account_id": accounts["1010"], "name": "Fremd"},
    ).status_code == 404
    for url in (
        f"/api/v1/cash-books/{book_id}",
        f"/api/v1/cash-books/{book_id}/entries",
        f"/api/v1/cash-books/{book_id}/closings",
        f"/api/v1/cash-books/{book_id}/report",
        f"/api/v1/cash-books/{book_id}/report.csv",
        f"/api/v1/cash-books/{book_id}/integrity",
    ):
        assert client.get(url, headers=FOREIGN).status_code == 404, url
    for method, url, body in (
        ("patch", f"/api/v1/cash-books/{book_id}", {"name": "x"}),
        ("post", f"/api/v1/cash-books/{book_id}/entries", {}),
        ("post", f"/api/v1/cash-books/{book_id}/entries/{first_entry['id']}/reverse", None),
        ("post", f"/api/v1/cash-books/{book_id}/closings", {}),
    ):
        assert getattr(client, method)(url, headers=FOREIGN, json=body).status_code == 404, url


def test_journal_reversal_api_refuses_cash_booking(api) -> None:
    client, company_id, accounts, _, _ = api
    book = _api_book(client, company_id, accounts)
    entry = client.get(
        f"/api/v1/cash-books/{book['id']}/entries", headers=WRITER
    ).get_json()["entries"][0]
    response = client.post(
        f"/api/v1/journal-entries/{entry['journal_entry_id']}/reverse",
        headers=WRITER,
        json={"reversal_date": TODAY.isoformat()},
    )
    assert response.status_code == 422
    assert "Kassenbuch" in response.get_json()["error"]


def test_audit_package_contains_the_cash_book_tables(api) -> None:
    client, company_id, accounts, _, _ = api
    book = _api_book(client, company_id, accounts)
    client.post(
        f"/api/v1/cash-books/{book['id']}/closings",
        headers=WRITER,
        json={"closing_date": TODAY.isoformat(), "counted_balance": "100.00"},
    )
    package = client.get(
        "/api/v1/exports/audit-package.zip",
        headers=WRITER,
        query_string={"company_id": company_id, "include_documents": "false"},
    )
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        books = json.loads(archive.read("data/cash_books.json"))
        entries = json.loads(archive.read("data/cash_entries.json"))
        closings = json.loads(archive.read("data/cash_closings.json"))
        catalog = json.loads(archive.read("schema/field_catalog.json"))
        manifest = json.loads(archive.read("manifest.json"))
    assert [item["name"] for item in books] == ["Hauptkasse"]
    assert [item["entry_number"] for item in entries] == [1]
    assert len(entries[0]["content_hash"]) == 64
    assert closings[0]["counted_balance"] == "100.00"
    catalog_text = json.dumps(catalog)
    assert all(name in catalog_text for name in ("cash_books", "cash_entries", "cash_closings"))
    assert "vorherigen Eintrags derselben Kasse" in catalog_text
    assert manifest["table_counts"]["cash_entries"] == 1


def test_audit_package_filters_cash_entries_by_date(api) -> None:
    client, company_id, accounts, _, _ = api
    _api_book(client, company_id, accounts)
    package = client.get(
        "/api/v1/exports/audit-package.zip",
        headers=WRITER,
        query_string={
            "company_id": company_id,
            "include_documents": "false",
            "date_from": TODAY.isoformat(),
        },
    )
    assert package.status_code == 200
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        assert json.loads(archive.read("data/cash_entries.json")) == []
        assert len(json.loads(archive.read("data/cash_books.json"))) == 1


# ---------------------------------------------------------------------------
# MCP und Chat
# ---------------------------------------------------------------------------


class _RecordingHttp:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def call(self, method, path, *, params=None, json_body=None) -> ApiResponse:
        self.calls.append((method, path, params, json_body))
        return ApiResponse(
            status=200, text='{"ok": true}', content_type="application/json", json={"ok": True}
        )


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "list_cash_books",
            {"company_id": 1},
            ("GET", "/cash-books", {"company_id": 1}, None),
        ),
        (
            "create_cash_book",
            {"company_id": 1, "account_id": 2, "name": "Kasse"},
            ("POST", "/cash-books", None, {"company_id": 1, "account_id": 2, "name": "Kasse"}),
        ),
        (
            "update_cash_book",
            {"cash_book_id": 3, "name": "Neu"},
            ("PATCH", "/cash-books/3", None, {"name": "Neu"}),
        ),
        (
            "list_cash_entries",
            {"cash_book_id": 3, "limit": 5},
            ("GET", "/cash-books/3/entries", {"limit": 5}, None),
        ),
        (
            "create_cash_entry",
            {"cash_book_id": 3, "entry_type": "receipt", "amount": "1.00"},
            ("POST", "/cash-books/3/entries", None, {"entry_type": "receipt", "amount": "1.00"}),
        ),
        (
            "reverse_cash_entry",
            {"cash_book_id": 3, "entry_id": 9},
            ("POST", "/cash-books/3/entries/9/reverse", None, None),
        ),
        (
            "list_cash_closings",
            {"cash_book_id": 3},
            ("GET", "/cash-books/3/closings", {}, None),
        ),
        (
            "create_cash_closing",
            {"cash_book_id": 3, "closing_date": "2026-01-01", "counted_balance": "5.00"},
            (
                "POST",
                "/cash-books/3/closings",
                None,
                {"closing_date": "2026-01-01", "counted_balance": "5.00"},
            ),
        ),
        (
            "get_cash_report",
            {"cash_book_id": 3, "date_from": "2026-01-01"},
            ("GET", "/cash-books/3/report", {"date_from": "2026-01-01"}, None),
        ),
        (
            "export_cash_report_csv",
            {"cash_book_id": 3, "download_link": True},
            ("GET", "/cash-books/3/report.csv", {"download_link": True}, None),
        ),
        ("verify_cash_book", {"cash_book_id": 3}, ("GET", "/cash-books/3/integrity", None, None)),
    ],
)
def test_mcp_tools_forward_to_the_api(tool, arguments, expected) -> None:
    http = _RecordingHttp()
    server = MCPServer(http=http)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    assert response["result"].get("isError") is not True
    assert http.calls[-1] == expected


def test_chat_confirms_writing_tools_and_runs_reading_tools_directly() -> None:
    writing = {
        "create_cash_book",
        "update_cash_book",
        "create_cash_entry",
        "reverse_cash_entry",
        "create_cash_closing",
    }
    reading = {
        "list_cash_books",
        "list_cash_entries",
        "list_cash_closings",
        "get_cash_report",
        "export_cash_report_csv",
        "verify_cash_book",
    }
    assert all(chat_tool_requires_confirmation(name) for name in writing)
    assert not any(chat_tool_requires_confirmation(name) for name in reading)
    assert (writing | reading) <= set(TOOLS_BY_NAME)
    assert not any(chat_tool_blocked(name) for name in writing | reading)


# ---------------------------------------------------------------------------
# Oberfläche
# ---------------------------------------------------------------------------


def _login(app, username: str, role: str, tenant_id: int | None = None):
    with app.extensions["db_session_factory"]() as db:
        db.add(
            User(
                username=username,
                password_hash=hash_password("geheim123"),
                role=role,
                tenant_id=tenant_id,
            )
        )
        db.commit()
    client = app.test_client()
    response = client.post("/auth/login", data={"username": username, "password": "geheim123"})
    assert response.status_code == 302
    return client


def test_ui_flow(api) -> None:
    _, company_id, accounts, tax_ids, app = api
    client = _login(app, "ui-admin", "Admin")

    empty = client.get(f"/kassenbuch?company_id={company_id}")
    assert empty.status_code == 200
    text_empty = empty.get_data(as_text=True)
    assert "Kasse anlegen" in text_empty
    assert "keine TSE" in text_empty
    assert "/kassenbuch" in client.get(f"/bank?company_id={company_id}").get_data(as_text=True)

    created = client.post(
        "/kassenbuch/anlegen",
        data={
            "company_id": company_id,
            "name": "Hauptkasse",
            "account_id": accounts["1000"],
            "opening_amount": "1.234,50",
            "opening_date": THREE_DAYS_AGO.isoformat(),
            "opening_contra_account_id": accounts["9000"],
        },
        follow_redirects=True,
    )
    page = created.get_data(as_text=True)
    assert "Kasse Hauptkasse wurde angelegt." in page
    assert "1.234,50" in page
    with app.extensions["db_session_factory"]() as db:
        book_id = db.execute(select(CashBook.id)).scalar_one()

    booked = client.post(
        f"/kassenbuch/{book_id}/eintrag",
        data={
            "entry_type": "receipt",
            "entry_date": THREE_DAYS_AGO.isoformat(),
            "amount": "119,00",
            "description": "Barverkauf Laden",
            "contra_account_id": accounts["8400"],
            "tax_code_id": tax_ids["ust"],
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Eintrag Nr. 2 wurde erfasst und gebucht." in booked
    assert "Barverkauf Laden" in booked
    assert "1.353,50" in booked

    refused = client.post(
        f"/kassenbuch/{book_id}/eintrag",
        data={
            "entry_type": "payment",
            "entry_date": TODAY.isoformat(),
            "amount": "99999,00",
            "description": "Zu viel",
            "contra_account_id": accounts["4900"],
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Betriebsprüfung" in refused

    with app.extensions["db_session_factory"]() as db:
        entry_id = db.execute(
            select(CashEntry.id).where(CashEntry.entry_number == 2)
        ).scalar_one()
    reversed_page = client.post(
        f"/kassenbuch/{book_id}/eintrag/{entry_id}/storno", follow_redirects=True
    ).get_data(as_text=True)
    assert "Storno erfasst als Eintrag Nr. 3." in reversed_page
    assert "storniert durch Nr. 3" in reversed_page

    closed = client.post(
        f"/kassenbuch/{book_id}/abschluss",
        data={
            "closing_date": TODAY.isoformat(),
            "counted_balance": "1.230,00",
            "difference_account_id": accounts["4990"],
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Kassensturz vom" in closed
    assert "-4,50" in closed

    report = client.get(f"/kassenbuch/{book_id}/bericht", query_string={"date_from": ""})
    assert report.status_code == 200
    report_text = report.get_data(as_text=True)
    assert "Kassenbericht Hauptkasse" in report_text
    assert "Tagessalden" in report_text
    assert "Einzelzeilen" in report_text
    assert "1.234,50" in report_text
    assert "keine TSE" in report_text

    exported = client.get(f"/kassenbuch/{book_id}/bericht.csv", query_string={"date_from": ""})
    assert exported.status_code == 200
    assert exported.mimetype == "text/csv"
    assert "Endbestand" in exported.get_data(as_text=True)

    check = client.get(f"/kassenbuch/{book_id}/pruefung")
    assert check.status_code == 200
    check_text = check.get_data(as_text=True)
    assert "Intakt" in check_text
    assert "Keine Befunde" in check_text

    edited = client.post(
        f"/kassenbuch/{book_id}/aendern",
        data={"name": "Ladenkasse", "is_active": "1"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Ladenkasse" in edited


def test_ui_report_defaults_to_the_current_month(api) -> None:
    _, company_id, accounts, _, app = api
    client = _login(app, "ui-admin", "Admin")
    client.post(
        "/kassenbuch/anlegen",
        data={
            "company_id": company_id,
            "name": "Hauptkasse",
            "account_id": accounts["1000"],
            "opening_amount": "10,00",
            "opening_date": TODAY.isoformat(),
            "opening_contra_account_id": accounts["9000"],
        },
    )
    with app.extensions["db_session_factory"]() as db:
        book_id = db.execute(select(CashBook.id)).scalar_one()
    page = client.get(f"/kassenbuch/{book_id}/bericht").get_data(as_text=True)
    assert f'value="{TODAY.replace(day=1).isoformat()}"' in page
    assert f'value="{TODAY.isoformat()}"' in page


def test_ui_reports_a_broken_date_and_inverted_range(api) -> None:
    _, company_id, accounts, _, app = api
    client = _login(app, "ui-admin", "Admin")
    client.post(
        "/kassenbuch/anlegen",
        data={"company_id": company_id, "name": "Kasse", "account_id": accounts["1000"]},
    )
    with app.extensions["db_session_factory"]() as db:
        book_id = db.execute(select(CashBook.id)).scalar_one()
    broken = client.post(
        f"/kassenbuch/{book_id}/eintrag",
        data={
            "entry_type": "receipt",
            "entry_date": "kein-datum",
            "amount": "1,00",
            "description": "x",
            "contra_account_id": accounts["8400"],
        },
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "gültiges Datum" in broken
    bad_amount = client.post(
        f"/kassenbuch/{book_id}/abschluss",
        data={"closing_date": TODAY.isoformat(), "counted_balance": "abc"},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Dezimalzahl" in bad_amount
    inverted = client.get(
        f"/kassenbuch/{book_id}/bericht",
        query_string={"date_from": TODAY.isoformat(), "date_to": YESTERDAY.isoformat()},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "Von-Datum" in inverted


def test_ui_shows_deviation_when_postings_bypass_the_cash_book(api) -> None:
    client_api, company_id, accounts, _, app = api
    book = _api_book(client_api, company_id, accounts)
    with app.extensions["db_session_factory"]() as db:
        create_journal_entry(
            session=db,
            payload=JournalEntryInput(
                company_id=company_id,
                entry_date=TODAY,
                description="Am Kassenbuch vorbei",
                status="posted",
                changed_by="pytest",
                lines=[
                    JournalLineInput(account_id=accounts["1000"], debit_amount=Decimal("5.00")),
                    JournalLineInput(account_id=accounts["8400"], credit_amount=Decimal("5.00")),
                ],
            ),
        )
    client = _login(app, "ui-admin", "Admin")
    page = client.get(
        f"/kassenbuch?company_id={company_id}&cash_book_id={book['id']}"
    ).get_data(as_text=True)
    assert "Abweichung" in page
    check = client.get(f"/kassenbuch/{book['id']}/pruefung").get_data(as_text=True)
    assert "Am Kassenbuch vorbei" in check
    assert "Intakt" in check
    assert "weicht ab" in check


def test_ui_read_only_role_sees_no_forms_and_cannot_write(api) -> None:
    client_api, company_id, accounts, _, app = api
    book = _api_book(client_api, company_id, accounts)
    with app.extensions["db_session_factory"]() as db:
        tenant_id = db.get(Company, company_id).tenant_id
    client = _login(app, "ui-pruefer", "Pruefer", tenant_id)

    page = client.get(f"/kassenbuch?company_id={company_id}").get_data(as_text=True)
    assert "Hauptkasse" in page
    assert "Eintrag erfassen" not in page
    assert "Stornieren" not in page

    response = client.post(
        f"/kassenbuch/{book['id']}/eintrag",
        data={
            "entry_type": "receipt",
            "entry_date": TODAY.isoformat(),
            "amount": "1,00",
            "description": "Nicht erlaubt",
            "contra_account_id": accounts["8400"],
        },
    )
    assert response.status_code in {302, 403}
    with app.extensions["db_session_factory"]() as db:
        assert db.execute(select(func.count(CashEntry.id))).scalar_one() == 1


def test_ui_keeps_tenants_apart(api) -> None:
    client_api, company_id, accounts, _, app = api
    book = _api_book(client_api, company_id, accounts)
    with app.extensions["db_session_factory"]() as db:
        other_tenant = db.execute(
            select(Tenant.id).where(Tenant.name == "Fremd GmbH Mandant")
        ).scalar_one()
    client = _login(app, "ui-fremd", "Buchhalter", other_tenant)

    assert client.get(f"/kassenbuch/{book['id']}/bericht").status_code == 404
    assert client.get(f"/kassenbuch/{book['id']}/bericht.csv").status_code == 404
    assert client.get(f"/kassenbuch/{book['id']}/pruefung").status_code == 404
    assert client.post(f"/kassenbuch/{book['id']}/abschluss", data={}).status_code == 404
    assert client.post(
        "/kassenbuch/anlegen",
        data={"company_id": company_id, "name": "x", "account_id": accounts["1010"]},
    ).status_code == 404


def test_journal_page_hides_reversal_for_cash_bookings(api) -> None:
    client_api, company_id, accounts, _, app = api
    book = _api_book(client_api, company_id, accounts)
    entry = client_api.get(
        f"/api/v1/cash-books/{book['id']}/entries", headers=WRITER
    ).get_json()["entries"][0]
    client = _login(app, "ui-admin", "Admin")
    page = client.get(f"/buchungen/{entry['journal_entry_id']}").get_data(as_text=True)
    assert "Kassenbuch" in page
    assert "Stornieren" not in page


def test_created_at_is_part_of_the_hash(session, seed) -> None:
    book = _book(session, seed)
    entry = session.execute(select(CashEntry)).scalar_one()
    original = entry.content_hash
    entry.created_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    assert calculate_cash_entry_hash(entry) != original
    session.rollback()
    assert book.id is not None
