"""Ausgangsrechnungen: Summen, Nummernkreis, Ausstellen, Storno, Ausgabe und Schnittstellen."""

from __future__ import annotations

import base64
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from alembic import command
from pypdf import PdfReader
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app import create_app
from app.auth import hash_api_token, hash_password
from app.db import _alembic_config, _alembic_head_revision, create_session_factory
from app.services.company_master_data import (
    CompanyMasterDataError,
    resolve_seller,
    seller_party,
    update_company_master_data,
)
from app.services.compliance_integrity import verify_compliance_integrity
from app.services.einvoice_export import (
    INVOICE_TYPE_CREDIT_NOTE,
    InvoiceLine,
    OutgoingInvoice,
    Party,
    build_cii,
    build_ubl,
    compute_tax_groups,
)
from app.services.einvoice_import import ParsedInvoice, parse_einvoice
from app.services.incoming_invoice import IncomingInvoiceError
from app.services.journal_entries import (
    JournalEntryCreationError,
    amend_journal_entry,
    finalize_journal_entry,
    reverse_journal_entry,
)
from app.services.open_items import settle_open_item
from app.services.partners import create_partner
from app.services.periods import lock_period
from app.services.sales_invoice_pdf import pdf_text
from app.services.sales_invoices import (
    SalesInvoiceError,
    cancel_sales_invoice,
    compute_invoice_totals,
    create_sales_invoice,
    delete_sales_invoice,
    einvoice_hints,
    invoice_currency,
    issue_problems,
    issue_sales_invoice,
    next_invoice_number,
    update_sales_invoice,
)
from app.services.vat_returns import compute_vat_return
from domain.models import (
    Account,
    AuditLog,
    BusinessPartner,
    Company,
    Document,
    JournalEntry,
    JournalEntryLine,
    OpenItem,
    Period,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceSequence,
    TaxCode,
    Tenant,
    User,
)

VALID_IBAN = "DE89 3704 0044 0532 0130 00"
SERVICE_DAY = date(2026, 3, 10)
ISSUE_DAY = date(2026, 3, 15)


@pytest.fixture()
def session_factory(tmp_path: Path):
    return create_session_factory(f"sqlite+pysqlite:///{tmp_path / 'invoices.db'}")


@pytest.fixture()
def session(session_factory) -> Session:
    with session_factory() as db:
        yield db


@pytest.fixture()
def upload_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


class _Books:
    """Gesellschaft mit Konten, Steuercodes und einem Kunden."""

    def __init__(self, session: Session, name: str = "Rechnung GmbH") -> None:
        tenant = Tenant(name=f"{name} Mandant")
        company = Company(
            tenant=tenant,
            name=name,
            currency_code="EUR",
            street="Hauptstraße 1",
            postal_code="10115",
            city="Berlin",
            country_code="DE",
            vat_id="DE123456789",
            tax_number="30/123/45678",
            phone="+49 30 123456",
            email="rechnung@example.org",
            website="https://example.org",
            register_court="Amtsgericht Berlin",
            register_number="HRB 12345",
            managing_directors="Erika Muster",
            bank_name="Musterbank",
            iban="DE89370400440532013000",
            bic="COBADEFFXXX",
        )
        session.add_all([tenant, company])
        session.flush()
        self.session = session
        self.company = company
        self.accounts: dict[str, Account] = {}
        for code, account_name, account_type, subledger in (
            ("1200", "Bank", "asset", None),
            ("1400", "Forderungen aus Lieferungen und Leistungen", "asset", "debtor"),
            ("1771", "Umsatzsteuer 7 %", "liability", None),
            ("1776", "Umsatzsteuer 19 %", "liability", None),
            ("8125", "Steuerfreie innergemeinschaftliche Lieferungen", "income", None),
            ("8120", "Steuerfreie Umsätze § 4 Nr. 1a UStG", "income", None),
            ("8337", "Erlöse § 13b UStG", "income", None),
            ("8150", "Sonstige steuerfreie Umsätze", "income", None),
            ("8300", "Erlöse 7 %", "income", None),
            ("8400", "Erlöse 19 %", "income", None),
            ("8401", "Erlöse 19 % Beratung", "income", None),
            ("4200", "Miete", "expense", None),
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
        self.tax: dict[str, TaxCode] = {}
        for code, rate, account_code, kind in (
            ("USt19", Decimal("19.00"), "1776", "output"),
            ("USt7", Decimal("7.00"), "1771", "output"),
            ("frei", Decimal("0.00"), None, "output"),
            ("VSt19", Decimal("19.00"), None, "input"),
        ):
            tax_code = TaxCode(
                tenant_id=tenant.id,
                company_id=company.id,
                code=code,
                rate=rate,
                kind=kind,
                vat_account_id=self.accounts[account_code].id if account_code else None,
            )
            session.add(tax_code)
            self.tax[code] = tax_code
        session.commit()
        self.customer = self.partner("Kunde AG")

    def partner(self, name: str, **fields) -> BusinessPartner:
        values = {
            "street": "Kundenweg 2",
            "postal_code": "50667",
            "city": "Köln",
            "email": "buchhaltung@kunde.example",
            **fields,
        }
        partner = create_partner(
            session=self.session,
            company=self.company,
            changed_by="pytest",
            name=name,
            is_customer=True,
            **values,
        )
        self.session.commit()
        return partner

    def line(self, *, tax: str | None = "USt19", account: str = "8400", **fields) -> dict:
        return {
            "description": "Beratung",
            "quantity": "2",
            "unit_price": "100.00",
            "tax_code_id": self.tax[tax].id if tax else None,
            "revenue_account_id": self.accounts[account].id,
            **fields,
        }

    def draft(self, *, lines: list[dict] | None = None, **fields) -> SalesInvoice:
        values = {
            "partner_id": self.customer.id,
            "issue_date": ISSUE_DAY,
            "service_date": SERVICE_DAY,
            "lines": lines if lines is not None else [self.line()],
            **fields,
        }
        return create_sales_invoice(
            session=self.session, company=self.company, changed_by="pytest", **values
        )

    def issued(self, upload_dir: Path, **fields) -> SalesInvoice:
        draft = self.draft(**fields)
        return issue_sales_invoice(
            session=self.session,
            invoice_id=draft.id,
            changed_by="pytest",
            upload_dir=upload_dir,
        )


def _entry_lines(session: Session, entry_id: int) -> list[JournalEntryLine]:
    return list(
        session.execute(
            select(JournalEntryLine)
            .where(JournalEntryLine.journal_entry_id == entry_id)
            .order_by(JournalEntryLine.line_number)
        ).scalars()
    )


def _ustva(session: Session, company_id: int) -> dict[str, Decimal]:
    return {
        row.kennziffer: row.amount
        for row in compute_vat_return(
            session=session,
            company_id=company_id,
            date_from=date(2026, 3, 1),
            date_to=date(2026, 3, 31),
        )
    }


def _document_bytes(session: Session, document_id: int) -> bytes:
    return Path(session.get(Document, document_id).storage_key).read_bytes()


def _pdf_text(content: bytes) -> str:
    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() for page in reader.pages)


def _xml_text(root: ET.Element, *path: str) -> str | None:
    """Text des ersten Elements entlang lokaler Namen, unabhängig vom Namespace."""
    current = root
    for name in path:
        current = next(
            (child for child in current if child.tag.rsplit("}", 1)[-1] == name), None
        )
        if current is None:
            return None
    return current.text


# ---------------------------------------------------------------------------
# Summen und Rundung
# ---------------------------------------------------------------------------


def test_totals_round_tax_per_rate_not_per_line(session: Session) -> None:
    books = _Books(session)
    draft = books.draft(
        lines=[
            # Je Position gerundet wären es 3 × 0,02 = 0,06 €; aus der Summe 0,06 × … ergibt 0,05.
            books.line(quantity="1", unit_price="0.09"),
            books.line(quantity="1", unit_price="0.09"),
            books.line(quantity="1", unit_price="0.09"),
            books.line(tax="USt7", account="8300", quantity="1.5", unit_price="33.33"),
        ]
    )
    totals = compute_invoice_totals(draft.lines)
    assert draft.lines[3].net_amount == Decimal("50.00")
    assert [(group.rate, group.basis, group.tax_amount) for group in totals.groups] == [
        (Decimal("7.00"), Decimal("50.00"), Decimal("3.50")),
        (Decimal("19.00"), Decimal("0.27"), Decimal("0.05")),
    ]
    assert totals.net == Decimal("50.27")
    assert totals.tax == Decimal("3.55")
    assert totals.gross == Decimal("53.82")
    assert draft.gross_total == Decimal("53.82")


def test_tax_groups_agree_with_einvoice_rounding() -> None:
    groups = compute_tax_groups(
        [(Decimal("19.00"), Decimal("10.03")), (Decimal("19.00"), Decimal("10.03"))]
    )
    assert [(g.basis, g.tax_amount) for g in groups] == [(Decimal("20.06"), Decimal("3.81"))]


def test_line_validation_messages(session: Session) -> None:
    books = _Books(session)
    with pytest.raises(SalesInvoiceError, match="Beschreibung ist Pflicht"):
        books.draft(lines=[books.line(description=" ")])
    with pytest.raises(SalesInvoiceError, match="Menge darf nicht 0"):
        books.draft(lines=[books.line(quantity="0")])
    with pytest.raises(SalesInvoiceError, match="darf nicht negativ"):
        books.draft(lines=[books.line(unit_price="-1")])
    with pytest.raises(SalesInvoiceError, match="höchstens 4 Nachkommastellen"):
        books.draft(lines=[books.line(unit_price="1.23456")])
    with pytest.raises(SalesInvoiceError, match="UN/ECE"):
        books.draft(lines=[books.line(unit_code="STUECK")])
    with pytest.raises(SalesInvoiceError, match="Vorsteuercode"):
        books.draft(lines=[books.line(tax="VSt19")])
    with pytest.raises(SalesInvoiceError, match="kein Erlöskonto"):
        books.draft(lines=[books.line(account="4200")])
    with pytest.raises(SalesInvoiceError, match="Firmenwährung"):
        books.draft(currency_code="USD")
    with pytest.raises(SalesInvoiceError, match="Datum"):
        books.draft(service_date="10.03.2026")


def test_invoice_currency_is_company_currency(session: Session) -> None:
    books = _Books(session)
    assert invoice_currency(books.company) == "EUR"
    assert invoice_currency(books.company, "eur") == "EUR"
    assert books.draft().currency_code == "EUR"


# ---------------------------------------------------------------------------
# Nummernkreis
# ---------------------------------------------------------------------------


def test_number_format_prefix_and_year(session: Session) -> None:
    books = _Books(session)
    assert next_invoice_number(
        session=session, company=books.company, invoice_date=date(2026, 1, 1)
    ) == "RE-2026-0001"
    assert next_invoice_number(
        session=session, company=books.company, invoice_date=date(2026, 12, 31)
    ) == "RE-2026-0002"
    assert next_invoice_number(
        session=session, company=books.company, invoice_date=date(2027, 1, 1)
    ) == "RE-2027-0001"
    books.company.invoice_prefix = "AR"
    assert next_invoice_number(
        session=session, company=books.company, invoice_date=date(2026, 6, 1)
    ) == "AR-2026-0003"
    session.rollback()


def test_numbers_are_gapless_despite_drafts_and_failures(
    session: Session, upload_dir: Path
) -> None:
    books = _Books(session)
    first = books.issued(upload_dir)
    assert first.invoice_number == "RE-2026-0001"

    # Entwurf ohne Nummer darf gelöscht werden und hinterlässt keine Lücke.
    deleted = books.draft()
    assert deleted.invoice_number is None
    delete_sales_invoice(session=session, invoice=deleted, changed_by="pytest")

    # Scheitert das Ausstellen spät (Periode gesperrt), fällt die Nummer zurück.
    locked_draft = books.draft(issue_date=date(2026, 5, 20), service_date=date(2026, 5, 20))
    second = books.issued(upload_dir, issue_date=date(2026, 4, 2), service_date=date(2026, 4, 1))
    assert second.invoice_number == "RE-2026-0002"
    period = session.execute(
        select(Period).where(Period.start_date <= date(2026, 5, 20))
        .where(Period.end_date >= date(2026, 5, 20))
    ).scalar_one_or_none()
    if period is None:
        books.issued(upload_dir, issue_date=date(2026, 5, 2), service_date=date(2026, 5, 1))
        period = session.execute(
            select(Period).where(Period.start_date <= date(2026, 5, 20))
            .where(Period.end_date >= date(2026, 5, 20))
        ).scalar_one()
    expected_next = session.execute(
        select(SalesInvoiceSequence.last_number).where(
            SalesInvoiceSequence.company_id == books.company.id
        )
    ).scalar_one()
    lock_period(session=session, period_id=period.id, locked_by="pytest", reason="Test")
    with pytest.raises(SalesInvoiceError, match="gesperrt"):
        issue_sales_invoice(
            session=session,
            invoice_id=locked_draft.id,
            changed_by="pytest",
            upload_dir=upload_dir,
        )
    assert session.get(SalesInvoice, locked_draft.id).invoice_number is None
    assert session.get(SalesInvoice, locked_draft.id).status == "draft"
    assert session.execute(
        select(SalesInvoiceSequence.last_number).where(
            SalesInvoiceSequence.company_id == books.company.id
        )
    ).scalar_one() == expected_next


def test_parallel_issue_yields_unique_gapless_numbers(session_factory, upload_dir: Path) -> None:
    with session_factory() as db:
        books = _Books(db)
        draft_ids = [books.draft().id for _ in range(6)]

    errors: list[Exception] = []
    barrier = threading.Barrier(len(draft_ids))

    def worker(invoice_id: int) -> None:
        with session_factory() as db:
            barrier.wait()
            # SQLite meldet bei Schreibkonflikten „database is locked“; das zählt als Wartezeit.
            for attempt in range(20):
                try:
                    issue_sales_invoice(
                        session=db,
                        invoice_id=invoice_id,
                        changed_by="pytest",
                        upload_dir=upload_dir,
                    )
                    return
                except DBAPIError as exc:
                    db.rollback()
                    if "locked" not in str(exc) or attempt == 19:
                        errors.append(exc)
                        return
                except Exception as exc:  # noqa: BLE001 - wird unten ausgewertet
                    errors.append(exc)
                    return

    threads = [threading.Thread(target=worker, args=(draft_id,)) for draft_id in draft_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors

    with session_factory() as db:
        numbers = sorted(
            db.execute(select(SalesInvoice.invoice_number)).scalars().all()
        )
    assert numbers == [f"RE-2026-{index:04d}" for index in range(1, 7)]


# ---------------------------------------------------------------------------
# Pflichtangaben
# ---------------------------------------------------------------------------


def test_issue_lists_all_missing_mandatory_data(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    company = books.company
    company.street = company.postal_code = company.city = None
    company.vat_id = company.tax_number = None
    partner = books.partner("Ohne Anschrift", street=None, postal_code=None, city=None)
    draft = books.draft(partner_id=partner.id, service_date=None)

    with pytest.raises(SalesInvoiceError) as caught:
        issue_sales_invoice(
            session=session, invoice_id=draft.id, changed_by="pytest", upload_dir=upload_dir
        )
    problems = " ".join(caught.value.problems)
    assert "Anschrift der eigenen Firma unvollständig" in problems
    assert "Steuernummer oder USt-IdNr der eigenen Firma" in problems
    assert "Anschrift des Kunden unvollständig" in problems
    assert "Leistungsdatum oder Leistungszeitraum fehlt" in problems
    # Nichts ist passiert: keine Nummer, keine Buchung, kein Posten, keine Datei.
    session.expire_all()
    fresh = session.get(SalesInvoice, draft.id)
    assert fresh.status == "draft"
    assert fresh.invoice_number is None
    assert session.execute(select(JournalEntry)).scalars().first() is None
    assert session.execute(select(OpenItem)).scalars().first() is None
    assert not upload_dir.exists()


def test_seller_fallback_from_config_counts_for_mandatory_data(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'fallback.db'}",
            "SELLER_STREET": "Konfigweg 9",
            "SELLER_POSTAL_CODE": "20095",
            "SELLER_CITY": "Hamburg",
            "SELLER_VAT_ID": "DE111111111",
            "SELLER_COUNTRY_CODE": "AT",
        }
    )
    with app.app_context(), app.extensions["db_session_factory"]() as db:
        tenant = Tenant(name="Rückfall")
        company = Company(tenant=tenant, name="Rückfall GmbH", currency_code="EUR")
        db.add_all([tenant, company])
        db.commit()
        seller = resolve_seller(company)
        assert (seller.street, seller.city, seller.country_code, seller.vat_id) == (
            "Konfigweg 9",
            "Hamburg",
            "AT",
            "DE111111111",
        )
        company.street = "Stammdatenstraße 1"
        company.country_code = "DE"
        party = seller_party(company)
        # Ein gepflegtes Feld geht vor, leere bleiben beim Rückfallwert.
        assert (party.street, party.city, party.country_code) == (
            "Stammdatenstraße 1",
            "Hamburg",
            "DE",
        )


def test_service_period_validation(session: Session) -> None:
    books = _Books(session)

    def problems_for(**fields) -> str:
        draft = books.draft(service_date=None, **fields)
        return " ".join(issue_problems(session=session, invoice=draft))

    assert "braucht Beginn und Ende" in problems_for(service_period_from=date(2026, 3, 1))
    assert "liegt nach dem Ende" in problems_for(
        service_period_from=date(2026, 3, 31), service_period_to=date(2026, 3, 1)
    )
    both = books.draft(
        service_date=SERVICE_DAY,
        service_period_from=date(2026, 3, 1),
        service_period_to=date(2026, 3, 31),
    )
    assert "nicht beides" in " ".join(issue_problems(session=session, invoice=both))


def test_receivable_account_selection(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    second = Account(
        tenant_id=books.company.tenant_id,
        company_id=books.company.id,
        code="1410",
        name="Forderungen Ausland",
        account_type="asset",
        subledger="debtor",
    )
    session.add(second)
    session.commit()

    draft = books.draft()
    assert "Mehrere Debitoren-Sammelkonten" in " ".join(
        issue_problems(session=session, invoice=draft)
    )
    issued = issue_sales_invoice(
        session=session,
        invoice_id=draft.id,
        changed_by="pytest",
        upload_dir=upload_dir,
        receivable_account_id=second.id,
    )
    assert issued.receivable_account_id == second.id
    assert issued.open_item.account_id == second.id

    with pytest.raises(SalesInvoiceError, match="kein Debitoren-Sammelkonto"):
        books.draft(receivable_account_id=books.accounts["1200"].id)

    # Ohne Sammelkonto ein verständlicher Fehler.
    other = _Books(session, name="Ohne Sammelkonto GmbH")
    other.accounts["1400"].subledger = None
    session.commit()
    no_account = other.draft()
    assert "kein Debitoren-Sammelkonto" in " ".join(
        issue_problems(session=session, invoice=no_account)
    )


# ---------------------------------------------------------------------------
# Buchung und OPOS
# ---------------------------------------------------------------------------


def test_booking_and_open_item_match_invoice(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir,
        lines=[
            books.line(),
            books.line(account="8401", description="Workshop", quantity="1", unit_price="50.00"),
            books.line(tax="USt7", account="8300", description="Buch", quantity="3",
                       unit_price="10.00"),
        ],
        due_date=date(2026, 4, 1),
    )
    assert issued.status == "issued"
    assert issued.invoice_number == "RE-2026-0001"
    assert issued.net_total == Decimal("280.00")
    assert issued.tax_total == Decimal("49.60")
    assert issued.gross_total == Decimal("329.60")
    assert issued.recipient_name == "Kunde AG"
    assert issued.recipient_city == "Köln"
    assert issued.issued_by == "pytest"

    entry = session.get(JournalEntry, issued.journal_entry_id)
    assert entry.entry_date == ISSUE_DAY
    assert entry.service_date == SERVICE_DAY
    assert entry.source == "sales_invoice"
    assert "RE-2026-0001" in entry.description
    rows = {
        (line.account.code, line.debit_amount, line.credit_amount, line.partner_id)
        for line in _entry_lines(session, entry.id)
    }
    assert rows == {
        ("1400", Decimal("329.60"), Decimal("0.00"), books.customer.id),
        ("8400", Decimal("0.00"), Decimal("200.00"), books.customer.id),
        ("8401", Decimal("0.00"), Decimal("50.00"), books.customer.id),
        ("8300", Decimal("0.00"), Decimal("30.00"), books.customer.id),
        ("1776", Decimal("0.00"), Decimal("47.50"), None),
        ("1771", Decimal("0.00"), Decimal("2.10"), None),
    }

    item = issued.open_item
    assert item.reference == "RE-2026-0001"
    assert item.partner_id == books.customer.id
    assert item.original_amount == issued.gross_total
    assert item.open_amount == issued.gross_total
    assert item.due_date == date(2026, 4, 1)
    assert item.account.code == "1400"
    assert item.journal_entry_id == entry.id


def test_invoice_rounding_flows_into_booking_and_ustva(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    # Je Konto gerundet wären es 0,02 + 0,02 = 0,04 €; die Rechnung weist 0,03 € aus.
    issued = books.issued(
        upload_dir,
        lines=[
            books.line(quantity="1", unit_price="0.08"),
            books.line(account="8401", quantity="1", unit_price="0.08"),
        ],
    )
    assert issued.tax_total == Decimal("0.03")
    tax_lines = [
        line for line in _entry_lines(session, issued.journal_entry_id)
        if line.account.code == "1776"
    ]
    assert [line.credit_amount for line in tax_lines] == [Decimal("0.03")]

    assert _ustva(session, books.company.id)["83"] == Decimal("0.03")


def test_discount_line_books_debit_on_revenue(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir,
        lines=[
            books.line(),
            books.line(account="8401", description="Nachlass", quantity="-1",
                       unit_price="20.00"),
        ],
    )
    assert issued.net_total == Decimal("180.00")
    rows = {
        (line.account.code, line.debit_amount, line.credit_amount)
        for line in _entry_lines(session, issued.journal_entry_id)
    }
    assert ("8401", Decimal("20.00"), Decimal("0.00")) in rows
    assert ("1776", Decimal("0.00"), Decimal("34.20")) in rows


def test_due_date_from_partner_payment_term(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    partner = books.partner("Zahlziel GmbH", payment_term_days=14)
    issued = books.issued(upload_dir, partner_id=partner.id)
    assert issued.due_date == date(2026, 3, 29)
    assert issued.open_item.due_date == date(2026, 3, 29)
    # Ohne Angabe gilt die Kundennummer als Käuferreferenz (BT-10).
    assert issued.buyer_reference == partner.debtor_number


def test_partner_validation(session: Session) -> None:
    books = _Books(session)
    supplier = create_partner(
        session=session,
        company=books.company,
        changed_by="pytest",
        name="Lieferant",
        is_supplier=True,
    )
    session.commit()
    with pytest.raises(SalesInvoiceError, match="ist kein Kunde"):
        books.draft(partner_id=supplier.id)
    foreign = _Books(session, name="Fremd GmbH")
    with pytest.raises(SalesInvoiceError, match="nicht gefunden"):
        books.draft(partner_id=foreign.customer.id)
    with pytest.raises(SalesInvoiceError, match="Erlöskonto"):
        books.draft(lines=[{**books.line(), "revenue_account_id": foreign.accounts["8400"].id}])
    with pytest.raises(SalesInvoiceError, match="Steuercode"):
        books.draft(lines=[{**books.line(), "tax_code_id": foreign.tax["USt19"].id}])
    with pytest.raises(SalesInvoiceError, match="Debitoren-Sammelkonto"):
        books.draft(receivable_account_id=foreign.accounts["1400"].id)


# ---------------------------------------------------------------------------
# Unveränderlichkeit
# ---------------------------------------------------------------------------


def test_issued_invoice_is_immutable_in_service_and_database(
    session: Session, upload_dir: Path
) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir)
    with pytest.raises(SalesInvoiceError, match="unveränderlich"):
        update_sales_invoice(
            session=session, invoice=issued, changed_by="pytest", header_text="geändert"
        )
    with pytest.raises(SalesInvoiceError, match="unveränderlich"):
        delete_sales_invoice(session=session, invoice=issued, changed_by="pytest")
    with pytest.raises(SalesInvoiceError, match="unveränderlich"):
        issue_sales_invoice(
            session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir
        )

    invoice_id, line_id = issued.id, issued.lines[0].id
    for statement, params in (
        ("UPDATE sales_invoice SET net_total = 1 WHERE id = :id", {"id": invoice_id}),
        ("UPDATE sales_invoice SET status = 'draft' WHERE id = :id", {"id": invoice_id}),
        (
            "UPDATE sales_invoice SET status = 'cancelled', header_text = 'x' WHERE id = :id",
            {"id": invoice_id},
        ),
        ("DELETE FROM sales_invoice WHERE id = :id", {"id": invoice_id}),
        ("UPDATE sales_invoice_line SET quantity = 9 WHERE id = :id", {"id": line_id}),
        ("DELETE FROM sales_invoice_line WHERE id = :id", {"id": line_id}),
        (
            "INSERT INTO sales_invoice_line (tenant_id, sales_invoice_id, line_number, "
            "description, quantity, unit_code, unit_price, tax_rate, net_amount) "
            "SELECT tenant_id, sales_invoice_id, 99, 'x', 1, 'C62', 1, 0, 1 "
            "FROM sales_invoice_line WHERE id = :id",
            {"id": line_id},
        ),
    ):
        with pytest.raises(DBAPIError, match="immutable"):
            session.execute(text(statement), params)
        session.rollback()


def test_invoice_booking_cannot_be_reversed_or_amended_directly(
    session: Session, upload_dir: Path
) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir)
    with pytest.raises(JournalEntryCreationError, match="bitte die Rechnung stornieren"):
        reverse_journal_entry(
            session=session,
            journal_entry_id=issued.journal_entry_id,
            reversal_date=date(2026, 3, 20),
            changed_by="pytest",
        )
    with pytest.raises(JournalEntryCreationError, match="Leistungsdatum und Partner"):
        amend_journal_entry(
            session=session,
            company_id=books.company.id,
            journal_entry_id=issued.journal_entry_id,
            changed_by="pytest",
            service_date=date(2026, 2, 1),
        )
    session.rollback()


# ---------------------------------------------------------------------------
# Steuerbehandlung
# ---------------------------------------------------------------------------


def _foreign_partner(books: _Books, name: str, country: str, vat_id: str | None) -> BusinessPartner:
    return books.partner(name, country_code=country, vat_id=vat_id)


TREATMENT_CASES = [
    pytest.param(
        "reverse_charge",
        "AE",
        "Steuerschuldnerschaft des Leistungsempfängers",
        "VATEX-EU-AE",
        "8337",
        "frei",
        "AT",
        "ATU12345678",
        id="reverse_charge",
    ),
    pytest.param(
        "intra_community",
        "K",
        "innergemeinschaftliche Lieferung",
        "VATEX-EU-IC",
        "8125",
        "frei",
        "AT",
        "ATU12345678",
        id="intra_community",
    ),
    pytest.param(
        "export",
        "G",
        "Ausfuhrlieferung",
        "VATEX-EU-G",
        "8120",
        None,
        "CH",
        None,
        id="export",
    ),
    pytest.param(
        "exempt",
        "E",
        "Steuerfreier Umsatz",
        None,
        "8150",
        "frei",
        "DE",
        None,
        id="exempt",
    ),
]


@pytest.mark.parametrize(
    "treatment,category,note,reason_code,account,tax,country,vat_id", TREATMENT_CASES
)
@pytest.mark.parametrize("syntax", ["ubl", "cii"])
def test_tax_free_treatments_show_no_tax(
    session: Session,
    upload_dir: Path,
    treatment: str,
    category: str,
    note: str,
    reason_code: str | None,
    account: str,
    tax: str | None,
    country: str,
    vat_id: str | None,
    syntax: str,
) -> None:
    books = _Books(session)
    partner = _foreign_partner(books, f"Kunde {treatment}", country, vat_id)
    issued = books.issued(
        upload_dir,
        partner_id=partner.id,
        tax_treatment=treatment,
        einvoice_syntax=syntax,
        lines=[books.line(tax=tax, account=account)],
    )
    assert issued.tax_total == Decimal("0.00")
    assert issued.gross_total == Decimal("200.00")
    assert issued.open_item.original_amount == Decimal("200.00")

    # Keine Steuerzeile in der Buchung.
    assert {line.account.code for line in _entry_lines(session, issued.journal_entry_id)} == {
        "1400",
        account,
    }

    xml = _document_bytes(session, issued.xml_document_id)
    parsed = parse_einvoice(xml)
    assert parsed.tax_total == Decimal("0.00")
    assert parsed.grand_total == Decimal("200.00")
    text_xml = xml.decode("utf-8")
    assert category in text_xml
    assert note in text_xml
    if reason_code:
        assert reason_code in text_xml

    pdf = _pdf_text(_document_bytes(session, issued.pdf_document_id))
    assert note in " ".join(pdf.split())
    assert "USt 0 %" in pdf


def test_small_business_invoice(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    books.company.is_small_business = True
    session.commit()
    # Kleinunternehmer: Vorgabe ist die passende Steuerbehandlung.
    draft = books.draft(lines=[books.line(tax=None)])
    assert draft.tax_treatment == "small_business"
    issued = issue_sales_invoice(
        session=session, invoice_id=draft.id, changed_by="pytest", upload_dir=upload_dir
    )
    assert issued.tax_total == Decimal("0.00")
    pdf = " ".join(_pdf_text(_document_bytes(session, issued.pdf_document_id)).split())
    assert "Kleinunternehmer nach § 19 UStG" in pdf
    xml = _document_bytes(session, issued.xml_document_id).decode("utf-8")
    assert "§ 19 UStG" in xml

    # Andere Steuerbehandlung ist für Kleinunternehmer gesperrt.
    standard = books.draft(tax_treatment="standard")
    assert "Kleinunternehmer" in " ".join(issue_problems(session=session, invoice=standard))
    with_tax = books.draft(lines=[books.line()])
    assert "erlaubt keinen Steuerausweis" in " ".join(
        issue_problems(session=session, invoice=with_tax)
    )


def test_small_business_treatment_needs_company_flag(session: Session) -> None:
    books = _Books(session)
    draft = books.draft(tax_treatment="small_business", lines=[books.line(tax=None)])
    assert "Kennzeichen in den Firmendaten" in " ".join(
        issue_problems(session=session, invoice=draft)
    )


def test_treatment_requirements(session: Session) -> None:
    books = _Books(session)

    def problems(partner: BusinessPartner, treatment: str) -> str:
        draft = books.draft(
            partner_id=partner.id, tax_treatment=treatment, lines=[books.line(tax="frei")]
        )
        return " ".join(issue_problems(session=session, invoice=draft))

    domestic = books.partner("Inland ohne USt-IdNr")
    assert "USt-IdNr des Kunden" in problems(domestic, "reverse_charge")
    assert "USt-IdNr des Kunden" in problems(domestic, "intra_community")
    assert "anderen EU-Mitgliedstaat" in problems(domestic, "intra_community")
    assert "außerhalb der EU" in problems(domestic, "export")

    swiss = _foreign_partner(books, "Schweiz AG", "CH", None)
    assert "anderen EU-Mitgliedstaat" in problems(swiss, "intra_community")
    assert "außerhalb der EU" not in problems(swiss, "export")

    books.company.vat_id = None
    session.commit()
    austrian = _foreign_partner(books, "Österreich GmbH", "AT", "ATU12345678")
    assert "eigene USt-IdNr" in problems(austrian, "intra_community")

    # Regelbesteuerung braucht einen Steuersatz.
    standard_free = books.draft(lines=[books.line(tax="frei")])
    assert "passende Steuerbehandlung" in " ".join(
        issue_problems(session=session, invoice=standard_free)
    )
    no_code = books.draft(lines=[books.line(tax=None)])
    assert "Steuercode fehlt" in " ".join(issue_problems(session=session, invoice=no_code))
    # Ohne Regelsteuer kein Steuercode mit Satz.
    reverse_taxed = books.draft(
        partner_id=austrian.id, tax_treatment="reverse_charge", lines=[books.line()]
    )
    assert "erlaubt keinen Steuerausweis" in " ".join(
        issue_problems(session=session, invoice=reverse_taxed)
    )


def test_tax_free_account_hint_and_ustva(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    # 8400 führt nicht zu Kz 41.
    partner = _foreign_partner(books, "EU Kunde", "AT", "ATU12345678")
    wrong = books.draft(
        partner_id=partner.id, tax_treatment="intra_community", lines=[books.line(tax="frei")]
    )
    assert any(
        "SKR03 8125" in hint for hint in einvoice_hints(session=session, invoice=wrong)
    )
    right = books.issued(
        upload_dir,
        partner_id=partner.id,
        tax_treatment="intra_community",
        lines=[books.line(tax="frei", account="8125")],
    )
    assert not any(
        "SKR03 8125" in hint for hint in einvoice_hints(session=session, invoice=right)
    )
    assert _ustva(session, books.company.id)["41"] == Decimal("200")


def test_einvoice_hints_name_missing_xrechnung_fields(session: Session) -> None:
    books = _Books(session)
    books.company.phone = books.company.email = books.company.iban = None
    partner = books.partner("Ohne Mail", email=None)
    session.commit()
    hints = " ".join(
        einvoice_hints(session=session, invoice=books.draft(partner_id=partner.id))
    )
    assert "Telefon, E-Mail, IBAN" in hints
    assert "E-Mail-Adresse des Kunden" in hints
    assert "Leitweg-ID" in hints


# ---------------------------------------------------------------------------
# Storno
# ---------------------------------------------------------------------------


def test_cancel_creates_cancellation_invoice(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir, lines=[books.line(), books.line(tax="USt7", account="8300")]
    )
    original_id = issued.id
    cancellation = cancel_sales_invoice(
        session=session,
        invoice_id=original_id,
        changed_by="pytest",
        upload_dir=upload_dir,
        cancellation_date=date(2026, 3, 20),
        reason="Falscher Empfänger",
    )
    session.expire_all()
    original = session.get(SalesInvoice, original_id)

    assert original.status == "cancelled"
    assert cancellation.status == "issued"
    assert cancellation.invoice_type == "cancellation"
    assert cancellation.invoice_number == "RE-2026-0002"
    assert cancellation.cancels_invoice_id == original_id
    assert original.cancellation.id == cancellation.id
    assert cancellation.gross_total == -original.gross_total
    assert cancellation.net_total == -original.net_total
    assert cancellation.tax_total == -original.tax_total
    assert [line.net_amount for line in cancellation.lines] == [
        -line.net_amount for line in original.lines
    ]
    assert cancellation.header_text == "Falscher Empfänger"

    # Buchung über reverse_journal_entry: gespiegelt, verknüpft, festgeschrieben.
    reversal = session.get(JournalEntry, cancellation.journal_entry_id)
    assert reversal.reversal_of_id == original.journal_entry_id
    assert reversal.is_finalized is True
    assert reversal.entry_date == date(2026, 3, 20)
    assert reversal.service_date == original.service_date
    assert sum(line.debit_amount for line in _entry_lines(session, reversal.id)) == (
        original.gross_total
    )

    # Der Posten ist ausgeglichen und über die Stornobuchung erklärt.
    item = original.open_item
    assert item.status == "settled"
    assert item.open_amount == Decimal("0.00")
    assert item.settlement_journal_entry_id == reversal.id
    assert cancellation.open_item_id == item.id

    # UStVA neutralisiert sich.
    assert _ustva(session, books.company.id)["83"] == Decimal("0.00")

    # Darstellung: negative Beträge, Bezug auf das Original; XML Typ 381 mit positiven Beträgen.
    pdf = " ".join(_pdf_text(_document_bytes(session, cancellation.pdf_document_id)).split())
    assert "Stornorechnung RE-2026-0002" in pdf
    assert "Storno der Rechnung RE-2026-0001 vom 15.03.2026" in pdf
    assert "-238,00" in pdf or "-214,00" in pdf or "-452,00" in pdf

    xml = _document_bytes(session, cancellation.xml_document_id)
    parsed = parse_einvoice(xml)
    assert parsed.type_code == INVOICE_TYPE_CREDIT_NOTE
    assert parsed.is_credit_note is True
    assert parsed.invoice_number == "RE-2026-0002"
    assert parsed.grand_total == -cancellation.gross_total
    root = ET.fromstring(xml)
    assert root.tag.endswith("}CreditNote")
    assert _xml_text(root, "CreditNoteTypeCode") == "381"
    assert (
        _xml_text(root, "BillingReference", "InvoiceDocumentReference", "ID") == "RE-2026-0001"
    )
    assert _xml_text(root, "BillingReference", "InvoiceDocumentReference", "IssueDate") == (
        "2026-03-15"
    )

    assert session.execute(
        select(AuditLog.action).where(
            AuditLog.entity_type == "sales_invoice", AuditLog.entity_id == str(original_id)
        )
    ).scalars().all() == ["created", "issued", "cancelled"]


def test_cancel_works_after_booking_was_finalized(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir)
    finalize_journal_entry(
        session=session, journal_entry_id=issued.journal_entry_id, changed_by="pytest"
    )
    cancellation = cancel_sales_invoice(
        session=session,
        invoice_id=issued.id,
        changed_by="pytest",
        upload_dir=upload_dir,
        cancellation_date=date(2026, 3, 20),
    )
    assert cancellation.status == "issued"
    session.expire_all()
    assert session.get(SalesInvoice, issued.id).status == "cancelled"
    assert verify_compliance_integrity(session=session, company_id=books.company.id).valid is True


def test_cancel_cii_uses_type_381_and_reference(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir, einvoice_syntax="cii")
    cancellation = cancel_sales_invoice(
        session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir,
        cancellation_date=date(2026, 3, 20),
    )
    xml = _document_bytes(session, cancellation.xml_document_id)
    parsed = parse_einvoice(xml)
    assert parsed.syntax == "CII"
    assert parsed.type_code == "381"
    assert parsed.grand_total == Decimal("238.00")
    root = ET.fromstring(xml)
    settlement = _find_local(root, "ApplicableHeaderTradeSettlement")
    assert _xml_text(settlement, "InvoiceReferencedDocument", "IssuerAssignedID") == (
        "RE-2026-0001"
    )


def _find_local(root: ET.Element, name: str) -> ET.Element:
    return next(el for el in root.iter() if el.tag.rsplit("}", 1)[-1] == name)


def test_cancel_rejected_after_partial_payment(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir)
    settle_open_item(
        session=session,
        open_item_id=issued.open_item_id,
        changed_by="pytest",
        amount=Decimal("10.00"),
    )
    with pytest.raises(SalesInvoiceError, match=r"\(teil-\)bezahlt.*10,00 EUR"):
        cancel_sales_invoice(
            session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir,
            cancellation_date=date(2026, 3, 20),
        )
    session.expire_all()
    fresh = session.get(SalesInvoice, issued.id)
    assert fresh.status == "issued"
    assert fresh.open_item.open_amount == Decimal("228.00")
    assert session.execute(
        select(SalesInvoice).where(SalesInvoice.invoice_type == "cancellation")
    ).scalars().first() is None


def test_cancel_guards(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    draft = books.draft()
    with pytest.raises(SalesInvoiceError, match="Entwurf"):
        cancel_sales_invoice(
            session=session, invoice_id=draft.id, changed_by="pytest", upload_dir=upload_dir
        )
    issued = books.issued(upload_dir)
    with pytest.raises(SalesInvoiceError, match="nicht vor dem Rechnungsdatum"):
        cancel_sales_invoice(
            session=session,
            invoice_id=issued.id,
            changed_by="pytest",
            upload_dir=upload_dir,
            cancellation_date=date(2026, 3, 1),
        )
    cancellation = cancel_sales_invoice(
        session=session,
        invoice_id=issued.id,
        changed_by="pytest",
        upload_dir=upload_dir,
        cancellation_date=date(2026, 3, 20),
    )
    with pytest.raises(SalesInvoiceError, match="bereits storniert"):
        cancel_sales_invoice(
            session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir,
            cancellation_date=date(2026, 3, 20),
        )
    with pytest.raises(SalesInvoiceError, match="Stornorechnung kann nicht"):
        cancel_sales_invoice(
            session=session, invoice_id=cancellation.id, changed_by="pytest",
            upload_dir=upload_dir, cancellation_date=date(2026, 3, 20),
        )


def test_failed_cancel_rolls_back_and_removes_files(
    session: Session, upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = _Books(session)
    issued = books.issued(upload_dir)
    files_before = sorted(upload_dir.rglob("*"))

    def explode(*_args, **_kwargs):
        raise RuntimeError("PDF kaputt")

    monkeypatch.setattr("app.services.sales_invoices.embed_xml_attachment", explode)
    with pytest.raises(RuntimeError):
        cancel_sales_invoice(
            session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir,
            cancellation_date=date(2026, 3, 20),
        )
    session.expire_all()
    assert session.get(SalesInvoice, issued.id).status == "issued"
    assert session.get(SalesInvoice, issued.id).open_item.status == "open"
    assert session.execute(
        select(JournalEntry).where(JournalEntry.reversal_of_id.is_not(None))
    ).scalars().first() is None
    assert session.execute(
        select(SalesInvoiceSequence.last_number)
    ).scalar_one() == 1
    assert sorted(upload_dir.rglob("*")) == files_before


def test_failed_issue_removes_written_files(
    session: Session, upload_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    books = _Books(session)
    draft = books.draft()

    def fail_on_audit(*_args, **_kwargs):
        raise RuntimeError("Audit kaputt")

    monkeypatch.setattr("app.services.sales_invoices._audit", fail_on_audit)
    with pytest.raises(RuntimeError):
        issue_sales_invoice(
            session=session, invoice_id=draft.id, changed_by="pytest", upload_dir=upload_dir
        )
    session.expire_all()
    fresh = session.get(SalesInvoice, draft.id)
    assert (fresh.status, fresh.invoice_number) == ("draft", None)
    assert session.execute(select(JournalEntry)).scalars().first() is None
    assert [path for path in upload_dir.rglob("*") if path.is_file()] == []


# ---------------------------------------------------------------------------
# Ausgabe: XML und PDF
# ---------------------------------------------------------------------------


def _full_outgoing() -> OutgoingInvoice:
    return OutgoingInvoice(
        invoice_number="RE-2026-0042",
        issue_date=date(2026, 3, 15),
        seller=Party(
            name="Verkauf GmbH",
            street="Hauptstraße 1",
            postal_code="10115",
            city="Berlin",
            vat_id="DE123456789",
            tax_number="30/123/45678",
            contact_name="Erika Muster",
            phone="+49 30 123456",
            email="rechnung@example.org",
            legal_registration_id="HRB 12345",
            legal_form_text="Amtsgericht Berlin HRB 12345",
        ),
        buyer=Party(
            name="Kunde AG",
            street="Kundenweg 2",
            postal_code="50667",
            city="Köln",
            vat_id="DE987654321",
            email="buchhaltung@kunde.example",
        ),
        lines=[
            InvoiceLine("Beratung", Decimal("1.5"), Decimal("33.3333"), Decimal("19.00"), "HUR"),
        ],
        buyer_reference="04011000-1234512345-06",
        due_date=date(2026, 4, 1),
        service_period_from=date(2026, 3, 1),
        service_period_to=date(2026, 3, 31),
        payment_iban="DE89370400440532013000",
        payment_bic="COBADEFFXXX",
        payment_account_name="Verkauf GmbH",
        payment_reference="RE-2026-0042",
        payment_terms="Zahlbar bis 01.04.2026 ohne Abzug.",
        notes=["Vielen Dank."],
    )


def test_ubl_carries_xrechnung_fields_in_schema_order() -> None:
    xml = build_ubl(_full_outgoing())
    root = ET.fromstring(xml)
    assert _xml_text(root, "CustomizationID") == (
        "urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0"
    )
    assert _xml_text(root, "ProfileID") == "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
    assert _xml_text(root, "DueDate") == "2026-04-01"
    assert _xml_text(root, "InvoiceTypeCode") == "380"
    assert _xml_text(root, "BuyerReference") == "04011000-1234512345-06"
    assert _xml_text(root, "InvoicePeriod", "StartDate") == "2026-03-01"
    assert _xml_text(root, "InvoicePeriod", "EndDate") == "2026-03-31"
    seller = _find_local(root, "AccountingSupplierParty")
    assert _xml_text(seller, "Party", "EndpointID") == "rechnung@example.org"
    assert _xml_text(seller, "Party", "Contact", "Name") == "Erika Muster"
    assert _xml_text(seller, "Party", "Contact", "Telephone") == "+49 30 123456"
    assert _xml_text(seller, "Party", "Contact", "ElectronicMail") == "rechnung@example.org"
    assert _xml_text(seller, "Party", "PartyLegalEntity", "CompanyID") == "HRB 12345"
    schemes = {
        _xml_text(el, "TaxScheme", "ID"): _xml_text(el, "CompanyID")
        for el in seller.iter()
        if el.tag.endswith("}PartyTaxScheme")
    }
    assert schemes == {"VAT": "DE123456789", "FC": "30/123/45678"}
    means = _find_local(root, "PaymentMeans")
    assert _xml_text(means, "PaymentMeansCode") == "58"
    assert _xml_text(means, "PaymentID") == "RE-2026-0042"
    assert _xml_text(means, "PayeeFinancialAccount", "ID") == "DE89370400440532013000"
    assert _xml_text(means, "PayeeFinancialAccount", "FinancialInstitutionBranch", "ID") == (
        "COBADEFFXXX"
    )
    line = _find_local(root, "InvoiceLine")
    quantity = _find_local(line, "InvoicedQuantity")
    assert quantity.text == "1.50"
    assert quantity.get("unitCode") == "HUR"
    assert _xml_text(line, "Price", "PriceAmount") == "33.3333"
    assert _xml_text(line, "LineExtensionAmount") == "50.00"

    # Reihenfolge laut UBL-Invoice-2.1.
    order = [child.tag.rsplit("}", 1)[-1] for child in root]
    expected = [
        "CustomizationID",
        "ProfileID",
        "ID",
        "IssueDate",
        "DueDate",
        "InvoiceTypeCode",
        "Note",
        "DocumentCurrencyCode",
        "BuyerReference",
        "InvoicePeriod",
        "AccountingSupplierParty",
        "AccountingCustomerParty",
        "PaymentMeans",
        "PaymentTerms",
        "TaxTotal",
        "LegalMonetaryTotal",
        "InvoiceLine",
    ]
    assert order == expected


def test_ubl_service_date_and_delivery_country_for_intra_community() -> None:
    invoice = _full_outgoing()
    invoice.service_period_from = invoice.service_period_to = None
    invoice.service_date = date(2026, 3, 10)
    invoice.tax_category = "K"
    invoice.exemption_reason = "Steuerfreie innergemeinschaftliche Lieferung"
    invoice.exemption_reason_code = "VATEX-EU-IC"
    invoice.buyer.country_code = "AT"
    invoice.lines[0].tax_rate = Decimal("0.00")
    root = ET.fromstring(build_ubl(invoice))
    assert _xml_text(root, "Delivery", "ActualDeliveryDate") == "2026-03-10"
    assert _xml_text(root, "Delivery", "DeliveryLocation", "Address", "Country",
                     "IdentificationCode") == "AT"
    category = _find_local(root, "TaxCategory")
    assert _xml_text(category, "ID") == "K"
    assert _xml_text(category, "TaxExemptionReasonCode") == "VATEX-EU-IC"
    assert _xml_text(category, "TaxExemptionReason").startswith("Steuerfreie")
    classified = _find_local(root, "ClassifiedTaxCategory")
    assert _xml_text(classified, "ID") == "K"
    assert _xml_text(classified, "TaxExemptionReason") is None


def test_cii_carries_addresses_lines_and_payment_in_schema_order() -> None:
    root = ET.fromstring(build_cii(_full_outgoing()))
    assert _xml_text(
        root, "ExchangedDocumentContext", "GuidelineSpecifiedDocumentContextParameter", "ID"
    ).endswith("xrechnung_3.0")
    transaction = _find_local(root, "SupplyChainTradeTransaction")
    order = [child.tag.rsplit("}", 1)[-1] for child in transaction]
    assert order == [
        "IncludedSupplyChainTradeLineItem",
        "ApplicableHeaderTradeAgreement",
        "ApplicableHeaderTradeDelivery",
        "ApplicableHeaderTradeSettlement",
    ]
    buyer = _find_local(root, "BuyerTradeParty")
    assert _xml_text(buyer, "PostalTradeAddress", "LineOne") == "Kundenweg 2"
    assert _xml_text(buyer, "PostalTradeAddress", "PostcodeCode") == "50667"
    assert _xml_text(buyer, "PostalTradeAddress", "CityName") == "Köln"
    assert _xml_text(buyer, "PostalTradeAddress", "CountryID") == "DE"
    assert _xml_text(buyer, "SpecifiedTaxRegistration", "ID") == "DE987654321"
    seller = _find_local(root, "SellerTradeParty")
    registrations = {
        el.find("{*}ID").get("schemeID"): el.find("{*}ID").text
        for el in seller
        if el.tag.endswith("}SpecifiedTaxRegistration")
    }
    assert registrations == {"VA": "DE123456789", "FC": "30/123/45678"}
    assert _xml_text(seller, "DefinedTradeContact", "PersonName") == "Erika Muster"
    settlement = _find_local(root, "ApplicableHeaderTradeSettlement")
    assert _xml_text(settlement, "PaymentReference") == "RE-2026-0042"
    assert _xml_text(
        settlement, "SpecifiedTradeSettlementPaymentMeans", "PayeePartyCreditorFinancialAccount",
        "IBANID",
    ) == "DE89370400440532013000"
    assert _xml_text(settlement, "BillingSpecifiedPeriod", "StartDateTime", "DateTimeString") == (
        "20260301"
    )
    assert _xml_text(
        settlement, "SpecifiedTradePaymentTerms", "DueDateDateTime", "DateTimeString"
    ) == "20260401"
    line = _find_local(root, "IncludedSupplyChainTradeLineItem")
    assert _xml_text(line, "SpecifiedTradeProduct", "Name") == "Beratung"
    assert _xml_text(line, "SpecifiedLineTradeDelivery", "BilledQuantity") == "1.50"
    assert _xml_text(
        line,
        "SpecifiedLineTradeSettlement",
        "SpecifiedTradeSettlementLineMonetarySummation",
        "LineTotalAmount",
    ) == "50.00"


@pytest.mark.parametrize("syntax,parsed_syntax", [("ubl", "UBL"), ("cii", "CII")])
def test_issued_xml_roundtrips_through_import(
    session: Session, upload_dir: Path, syntax: str, parsed_syntax: str
) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir,
        einvoice_syntax=syntax,
        lines=[books.line(), books.line(tax="USt7", account="8300")],
    )
    parsed: ParsedInvoice = parse_einvoice(_document_bytes(session, issued.xml_document_id))
    assert parsed.syntax == parsed_syntax
    assert parsed.invoice_number == issued.invoice_number
    assert parsed.issue_date == issued.issue_date
    assert parsed.seller_name == books.company.name
    assert parsed.net_total == issued.net_total
    assert parsed.tax_total == issued.tax_total
    assert parsed.grand_total == issued.gross_total
    assert parsed.is_credit_note is False
    assert {(line.rate, line.basis, line.tax_amount) for line in parsed.tax_lines} == {
        (Decimal("19.00"), Decimal("200.00"), Decimal("38.00")),
        (Decimal("7.00"), Decimal("200.00"), Decimal("14.00")),
    }


def test_incoming_credit_note_is_not_booked_as_invoice(session: Session, upload_dir: Path) -> None:
    from app.services.einvoice_import import book_einvoice

    books = _Books(session)
    issued = books.issued(upload_dir)
    cancellation = cancel_sales_invoice(
        session=session, invoice_id=issued.id, changed_by="pytest", upload_dir=upload_dir,
        cancellation_date=date(2026, 3, 20),
    )
    parsed = parse_einvoice(_document_bytes(session, cancellation.xml_document_id))
    with pytest.raises(IncomingInvoiceError, match="Gutschrift"):
        book_einvoice(
            session=session,
            company=books.company,
            invoice=parsed,
            expense_account_id=books.accounts["4200"].id,
            creditor_account_id=books.accounts["1400"].id,
            changed_by="pytest",
        )


def test_pdf_contains_mandatory_data_and_xml_attachment(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir,
        due_date=date(2026, 4, 1),
        header_text="Vielen Dank für den Auftrag.",
        footer_text="Es gelten unsere AGB.",
        lines=[
            books.line(description="Beratung März", quantity="2.5", unit_code="HUR"),
            books.line(tax="USt7", account="8300", description="Fachbuch"),
        ],
    )
    pdf_bytes = _document_bytes(session, issued.pdf_document_id)
    assert pdf_bytes.startswith(b"%PDF-")
    assert session.get(Document, issued.pdf_document_id).mime_type == "application/pdf"
    text_pdf = _pdf_text(pdf_bytes)
    flat = " ".join(text_pdf.split())
    for expected in (
        # Aussteller und Empfänger
        "Rechnung GmbH",
        "Hauptstraße 1",
        "10115 Berlin",
        "Kunde AG",
        "Kundenweg 2",
        "50667 Köln",
        # Steuernummer und USt-IdNr, Datum, Nummer
        "USt-IdNr: DE123456789",
        "Steuernummer: 30/123/45678",
        "15.03.2026",
        "RE-2026-0001",
        # Leistung und Zeitpunkt
        "Beratung März",
        "Fachbuch",
        "Leistungsdatum",
        "10.03.2026",
        # Entgelt und Steuer je Satz
        "USt 19 % auf 250,00",
        "47,50",
        "USt 7 % auf 200,00",
        "14,00",
        "511,50 EUR",
        # Zahlung, Bank, Register
        "01.04.2026",
        "IBAN DE89370400440532013000",
        "BIC COBADEFFXXX",
        "Musterbank",
        "Amtsgericht Berlin HRB 12345",
        "Geschäftsführung: Erika Muster",
        "Vielen Dank für den Auftrag.",
        "Es gelten unsere AGB.",
        "Seite 1 von 1",
    ):
        assert expected in flat, expected

    reader = PdfReader(BytesIO(pdf_bytes))
    attachments = reader.attachments
    assert list(attachments) == ["Rechnung_RE-2026-0001.xml"]
    assert attachments["Rechnung_RE-2026-0001.xml"][0] == _document_bytes(
        session, issued.xml_document_id
    )

    # Beide Belege hängen mit Hash an der Buchung.
    documents = session.execute(
        select(Document).where(Document.journal_entry_id == issued.journal_entry_id)
    ).scalars().all()
    assert {document.file_name for document in documents} == {
        "Rechnung_RE-2026-0001.pdf",
        "Rechnung_RE-2026-0001.xml",
    }
    for document in documents:
        assert len(document.file_sha256) == 64
        assert document.capture_source == "sales_invoice"
        assert document.document_date == ISSUE_DAY


def test_pdf_page_break_escapes_markup_and_handles_foreign_letters(
    session: Session, upload_dir: Path
) -> None:
    books = _Books(session)
    partner = books.partner("Łódź <b>Sp.</b> & Co")
    issued = books.issued(
        upload_dir,
        partner_id=partner.id,
        lines=[
            books.line(description=f"Position {number} <img src='x'/>", quantity="1")
            for number in range(1, 70)
        ],
    )
    reader = PdfReader(BytesIO(_document_bytes(session, issued.pdf_document_id)))
    assert len(reader.pages) >= 2
    first, last = reader.pages[0].extract_text(), reader.pages[-1].extract_text()
    # Tabellenkopf auf jeder Seite, Summe am Schluss, Seitenzahl korrekt.
    assert "Beschreibung" in first
    assert "Beschreibung" in last
    assert "Gesamtbetrag" in last
    assert f"Seite {len(reader.pages)} von {len(reader.pages)}" in last
    flat = " ".join(first.split())
    assert "Lódz <b>Sp.</b> & Co" in flat
    assert "Position 1 <img src='x'/>" in flat
    # Das XML behält den Namen unverändert.
    assert "Łódź" in _document_bytes(session, issued.xml_document_id).decode("utf-8")


def test_pdf_text_transliterates_only_what_is_needed() -> None:
    assert pdf_text("Müller & Söhne €") == "Müller & Söhne €"
    assert pdf_text("Łódź č") == "Lódz c"
    assert pdf_text("日本") == "??"


def test_period_shown_on_pdf_and_used_for_booking(session: Session, upload_dir: Path) -> None:
    books = _Books(session)
    issued = books.issued(
        upload_dir,
        service_date=None,
        service_period_from=date(2026, 2, 1),
        service_period_to=date(2026, 2, 28),
    )
    assert session.get(JournalEntry, issued.journal_entry_id).service_date == date(2026, 2, 28)
    flat = " ".join(_pdf_text(_document_bytes(session, issued.pdf_document_id)).split())
    assert "Leistungszeitraum" in flat
    assert "01.02.2026 bis 28.02.2026" in flat
    root = ET.fromstring(_document_bytes(session, issued.xml_document_id))
    assert _xml_text(root, "InvoicePeriod", "EndDate") == "2026-02-28"


# ---------------------------------------------------------------------------
# Firmenstammdaten
# ---------------------------------------------------------------------------


def test_master_data_validation_and_audit(session: Session) -> None:
    books = _Books(session)
    company = books.company
    assert update_company_master_data(
        session=session, company=company, changed_by="pytest", city="Potsdam", vat_id="de 123456789"
    )
    session.commit()
    assert company.city == "Potsdam"
    assert company.vat_id == "DE123456789"
    entry = session.execute(
        select(AuditLog).where(
            AuditLog.entity_type == "company", AuditLog.action == "master_data_updated"
        )
    ).scalar_one()
    assert entry.payload["before"]["city"] == "Berlin"
    assert entry.payload["after"]["city"] == "Potsdam"

    # Ohne Änderung kein neuer Eintrag.
    assert not update_company_master_data(
        session=session, company=company, changed_by="pytest", city="Potsdam"
    )
    for field, value, message in (
        ("email", "kein-at", "E-Mail"),
        ("vat_id", "DE12", "USt-IdNr"),
        ("country_code", "Deutschland", "ISO"),
        ("invoice_prefix", "RE-", "Buchstaben und Ziffern"),
        ("is_small_business", "ja", "true oder false"),
        ("iban", VALID_IBAN, "Bankdaten"),
        ("name", "Neu", "Nicht änderbare"),
    ):
        with pytest.raises(CompanyMasterDataError, match=message):
            update_company_master_data(
                session=session, company=company, changed_by="pytest", **{field: value}
            )
        session.rollback()
    # Leerer Präfix fällt auf den Standard zurück.
    update_company_master_data(
        session=session, company=company, changed_by="pytest", invoice_prefix=" "
    )
    assert company.invoice_prefix == "RE"


# ---------------------------------------------------------------------------
# Web, REST, MCP
# ---------------------------------------------------------------------------


def _app(tmp_path: Path, **config):
    return create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'invoices_app.db'}",
            "DOCUMENT_UPLOAD_DIR": str(tmp_path / "app-uploads"),
            **config,
        }
    )


def _admin_client(app):
    with app.extensions["db_session_factory"]() as db:
        db.add(
            User(
                username="admin",
                password_hash=hash_password("geheim123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db.commit()
    client = app.test_client()
    response = client.post("/auth/login", data={"username": "admin", "password": "geheim123"})
    assert response.status_code == 302
    return client


def _ids(books: _Books) -> dict:
    return {
        "company": books.company.id,
        "customer": books.customer.id,
        "revenue": books.accounts["8400"].id,
        "revenue7": books.accounts["8300"].id,
        "tax19": books.tax["USt19"].id,
        "tax7": books.tax["USt7"].id,
    }


def _api_payload(ids: dict, **fields) -> dict:
    return {
        "company_id": ids["company"],
        "partner_id": ids["customer"],
        "issue_date": "2026-03-15",
        "service_date": "2026-03-10",
        "due_date": "2026-04-01",
        "lines": [
            {
                "description": "Beratung",
                "quantity": "2",
                "unit_price": "100.00",
                "tax_code_id": ids["tax19"],
                "revenue_account_id": ids["revenue"],
            }
        ],
        **fields,
    }


def test_rest_api_lifecycle(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        ids = _ids(_Books(db))

    created = client.post("/api/v1/sales-invoices", json=_api_payload(ids))
    assert created.status_code == 201, created.get_json()
    invoice = created.get_json()
    invoice_id = invoice["id"]
    assert invoice["status"] == "draft"
    assert invoice["invoice_number"] is None
    assert invoice["gross_total"] == "238.00"
    assert invoice["partner_name"] == "Kunde AG"
    assert invoice["issue_problems"] == []
    assert isinstance(invoice["einvoice_hints"], list)

    patched = client.patch(
        f"/api/v1/sales-invoices/{invoice_id}",
        json={"buyer_reference": "04011000-1234512345-06", "header_text": "Danke"},
    )
    assert patched.status_code == 200
    assert patched.get_json()["buyer_reference"] == "04011000-1234512345-06"

    assert client.patch(
        f"/api/v1/sales-invoices/{invoice_id}", json={"lines": "x"}
    ).status_code == 422
    assert client.patch(
        f"/api/v1/sales-invoices/{invoice_id}", json={"unbekannt": 1}
    ).status_code == 422

    # Downloads gibt es erst nach dem Ausstellen.
    assert client.get(f"/api/v1/sales-invoices/{invoice_id}/pdf").status_code == 409

    issued = client.post(f"/api/v1/sales-invoices/{invoice_id}/issue", json={})
    assert issued.status_code == 200, issued.get_json()
    issued_body = issued.get_json()
    assert issued_body["status"] == "issued"
    assert issued_body["invoice_number"] == "RE-2026-0001"
    assert issued_body["journal_entry_id"]
    assert issued_body["open_item_id"]
    assert issued_body["payment_status"] == "open"

    # Ausgestellt: nicht mehr änderbar, nicht löschbar.
    assert client.patch(
        f"/api/v1/sales-invoices/{invoice_id}", json={"header_text": "x"}
    ).status_code == 409
    assert client.delete(f"/api/v1/sales-invoices/{invoice_id}").status_code == 409
    assert client.post(f"/api/v1/sales-invoices/{invoice_id}/issue", json={}).status_code == 409

    # Downloads
    pdf = client.get(f"/api/v1/sales-invoices/{invoice_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b"%PDF-")
    assert pdf.mimetype == "application/pdf"
    assert "Rechnung_RE-2026-0001.pdf" in pdf.headers["Content-Disposition"]
    xml = client.get(f"/api/v1/sales-invoices/{invoice_id}/xml")
    assert xml.status_code == 200
    assert parse_einvoice(xml.data).invoice_number == "RE-2026-0001"

    link = client.get(f"/api/v1/sales-invoices/{invoice_id}/pdf?download_link=1")
    assert link.status_code == 200
    assert link.get_json()["file_name"] == "Rechnung_RE-2026-0001.pdf"
    token_path = link.get_json()["download_path"]
    anonymous = app.test_client()
    assert anonymous.get(token_path).data.startswith(b"%PDF-")

    content = client.get(f"/api/v1/sales-invoices/{invoice_id}/content?format=xml")
    assert content.status_code == 200
    body = content.get_json()
    assert body["file_name"] == "Rechnung_RE-2026-0001.xml"
    assert base64.b64decode(body["content_base64"]) == xml.data
    assert body["integrity"]["matches"] is True
    assert body["download_url"]
    assert client.get(
        f"/api/v1/sales-invoices/{invoice_id}/content?format=doc"
    ).status_code == 400
    link_only = client.get(
        f"/api/v1/sales-invoices/{invoice_id}/content?format=pdf&download_link=1"
    ).get_json()
    assert "content_base64" not in link_only
    assert link_only["download_url"]

    # Liste und Filter
    listed = client.get(
        "/api/v1/sales-invoices",
        query_string={"company_id": ids["company"], "status": "issued"},
    )
    assert listed.status_code == 200
    assert listed.get_json()["total"] == 1
    assert client.get(
        "/api/v1/sales-invoices",
        query_string={"company_id": ids["company"], "status": "draft"},
    ).get_json()["total"] == 0
    assert client.get(
        "/api/v1/sales-invoices",
        query_string={"company_id": ids["company"], "partner_id": ids["customer"]},
    ).get_json()["total"] == 1
    assert client.get(
        "/api/v1/sales-invoices", query_string={"company_id": ids["company"], "status": "x"}
    ).status_code == 422
    assert client.get("/api/v1/sales-invoices").status_code == 400

    # Storno
    cancelled = client.post(
        f"/api/v1/sales-invoices/{invoice_id}/cancel",
        json={"cancellation_date": "2026-03-20", "reason": "Fehler"},
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    cancellation = cancelled.get_json()
    assert cancellation["invoice_type"] == "cancellation"
    assert cancellation["invoice_number"] == "RE-2026-0002"
    assert cancellation["gross_total"] == "-238.00"
    assert client.get(f"/api/v1/sales-invoices/{invoice_id}").get_json()["status"] == "cancelled"
    assert client.post(
        f"/api/v1/sales-invoices/{invoice_id}/cancel", json={}
    ).status_code == 409
    assert client.post(
        f"/api/v1/sales-invoices/{cancellation['id']}/cancel", json={}
    ).status_code == 409

    # Das Dokument der Rechnung ist gesperrt.
    doc_id = issued_body["pdf_document_id"]
    assert client.post(
        f"/api/v1/documents/{doc_id}/link", json={"journal_entry_id": None}
    ).status_code == 409
    assert client.post(
        f"/api/v1/documents/{doc_id}/replace",
        json={
            "file_name": "x.pdf",
            "mime_type": "application/pdf",
            "content_base64": issued_body and base64.b64encode(pdf.data).decode("ascii"),
        },
    ).status_code == 409


def test_rest_issue_reports_problems_and_delete_draft(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        ids = _ids(_Books(db))
    draft = client.post(
        "/api/v1/sales-invoices", json=_api_payload(ids, service_date=None)
    ).get_json()
    assert "Leistungsdatum oder Leistungszeitraum fehlt." in draft["issue_problems"]
    response = client.post(f"/api/v1/sales-invoices/{draft['id']}/issue", json={})
    assert response.status_code == 422
    assert "Leistungsdatum oder Leistungszeitraum fehlt." in response.get_json()["problems"]
    assert client.post(
        f"/api/v1/sales-invoices/{draft['id']}/issue", json={"issue_date": "x"}
    ).status_code == 400

    assert client.delete(f"/api/v1/sales-invoices/{draft['id']}").status_code == 200
    assert client.get(f"/api/v1/sales-invoices/{draft['id']}").status_code == 404
    assert client.post(
        "/api/v1/sales-invoices", json={"company_id": ids["company"]}
    ).status_code == 422
    assert client.post("/api/v1/sales-invoices", json={}).status_code == 400


def test_master_data_rest(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        company_id = _Books(db).company.id
    current = client.get(f"/api/v1/companies/{company_id}/master-data")
    assert current.status_code == 200
    body = current.get_json()
    assert body["vat_id"] == "DE123456789"
    assert body["iban"] == "DE89370400440532013000"
    assert body["invoice_prefix"] == "RE"

    updated = client.patch(
        f"/api/v1/companies/{company_id}/master-data",
        json={"city": "Potsdam", "invoice_prefix": "AR", "is_small_business": True},
    )
    assert updated.status_code == 200
    assert updated.get_json()["city"] == "Potsdam"
    assert updated.get_json()["changed"] is True
    assert client.patch(
        f"/api/v1/companies/{company_id}/master-data", json={"iban": VALID_IBAN}
    ).status_code == 422
    assert client.patch(
        f"/api/v1/companies/{company_id}/master-data", json={"email": "kaputt"}
    ).status_code == 422
    assert client.get("/api/v1/companies/9999/master-data").status_code == 404
    log = client.get(
        "/api/v1/audit-log",
        query_string={"company_id": company_id, "entity_type": "company"},
    )
    assert log.status_code == 200


def test_rights_and_tenant_separation(tmp_path: Path) -> None:
    app = _app(tmp_path, API_REQUIRE_AUTH=True)
    with app.extensions["db_session_factory"]() as db:
        books_a = _Books(db, name="A GmbH")
        books_b = _Books(db, name="B GmbH")
        ids_a, ids_b = _ids(books_a), _ids(books_b)
        for username, role, token, tenant_id in (
            ("buchhalter-a", "Buchhalter", "obk_buchhalter-a", books_a.company.tenant_id),
            ("pruefer-a", "Pruefer", "obk_pruefer-a", books_a.company.tenant_id),
            ("support-a", "Support", "obk_support-a", books_a.company.tenant_id),
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
        foreign = create_sales_invoice(
            session=db,
            company=books_b.company,
            changed_by="pytest",
            partner_id=books_b.customer.id,
            lines=[books_b.line()],
        )
        foreign_id = foreign.id

    client = app.test_client()
    writer = {"Authorization": "Bearer obk_buchhalter-a"}
    reader = {"Authorization": "Bearer obk_pruefer-a"}
    support = {"Authorization": "Bearer obk_support-a"}

    created = client.post("/api/v1/sales-invoices", json=_api_payload(ids_a), headers=writer)
    assert created.status_code == 201, created.get_json()
    invoice_id = created.get_json()["id"]

    # Prüfer und Support lesen nur.
    for headers in (reader, support):
        assert client.get(
            f"/api/v1/sales-invoices/{invoice_id}", headers=headers
        ).status_code == 200
        assert client.post(
            "/api/v1/sales-invoices", json=_api_payload(ids_a), headers=headers
        ).status_code == 403
        assert client.patch(
            f"/api/v1/sales-invoices/{invoice_id}", json={"header_text": "x"}, headers=headers
        ).status_code == 403
        assert client.delete(
            f"/api/v1/sales-invoices/{invoice_id}", headers=headers
        ).status_code == 403
        assert client.post(
            f"/api/v1/sales-invoices/{invoice_id}/issue", json={}, headers=headers
        ).status_code == 403
        assert client.post(
            f"/api/v1/sales-invoices/{invoice_id}/cancel", json={}, headers=headers
        ).status_code == 403
        assert client.patch(
            f"/api/v1/companies/{ids_a['company']}/master-data",
            json={"city": "X"},
            headers=headers,
        ).status_code == 403
        assert client.get(
            f"/api/v1/companies/{ids_a['company']}/master-data", headers=headers
        ).status_code == 200

    # Fremde Gesellschaft, fremde Rechnung, fremder Partner, fremdes Konto.
    assert client.get(
        "/api/v1/sales-invoices", query_string={"company_id": ids_b["company"]}, headers=writer
    ).status_code == 404
    assert client.get(f"/api/v1/sales-invoices/{foreign_id}", headers=writer).status_code == 404
    assert client.patch(
        f"/api/v1/sales-invoices/{foreign_id}", json={"header_text": "x"}, headers=writer
    ).status_code == 404
    assert client.delete(f"/api/v1/sales-invoices/{foreign_id}", headers=writer).status_code == 404
    assert client.post(
        f"/api/v1/sales-invoices/{foreign_id}/issue", json={}, headers=writer
    ).status_code == 404
    assert client.post(
        f"/api/v1/sales-invoices/{foreign_id}/cancel", json={}, headers=writer
    ).status_code == 404
    assert client.get(f"/api/v1/sales-invoices/{foreign_id}/pdf", headers=writer).status_code == 404
    assert client.get(
        f"/api/v1/companies/{ids_b['company']}/master-data", headers=writer
    ).status_code == 404
    assert client.post(
        "/api/v1/sales-invoices",
        json=_api_payload({**ids_b}, company_id=ids_a["company"]),
        headers=writer,
    ).status_code == 422
    mixed = _api_payload(ids_a)
    mixed["lines"][0]["revenue_account_id"] = ids_b["revenue"]
    assert client.post("/api/v1/sales-invoices", json=mixed, headers=writer).status_code == 422
    mixed = _api_payload(ids_a)
    mixed["lines"][0]["tax_code_id"] = ids_b["tax19"]
    assert client.post("/api/v1/sales-invoices", json=mixed, headers=writer).status_code == 422


def test_ui_smoke(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        ids = _ids(_Books(db))

    listing = client.get(f"/rechnungen?company_id={ids['company']}")
    assert listing.status_code == 200
    assert "Rechnungen" in listing.get_data(as_text=True)
    assert "/rechnungen/neu" in listing.get_data(as_text=True)

    new_form = client.get(
        f"/rechnungen/neu?company_id={ids['company']}&partner_id={ids['customer']}"
    )
    assert new_form.status_code == 200
    assert "Kunde AG" in new_form.get_data(as_text=True)

    created = client.post(
        "/rechnungen",
        data={
            "company_id": ids["company"],
            "partner_id": ids["customer"],
            "issue_date": "2026-03-15",
            "service_date": "2026-03-10",
            "tax_treatment": "standard",
            "einvoice_syntax": "ubl",
            "line_description": ["Beratung", "Fachbuch", ""],
            "line_quantity": ["2,5", "1", ""],
            "line_unit_code": ["HUR", "C62", "C62"],
            "line_unit_price": ["100,00", "1.000,50", ""],
            "line_tax_code_id": [ids["tax19"], ids["tax7"], ""],
            "line_revenue_account_id": [ids["revenue"], ids["revenue7"], ""],
        },
    )
    assert created.status_code == 302, created.get_data(as_text=True)
    detail_url = created.headers["Location"]
    detail = client.get(detail_url)
    assert detail.status_code == 200
    page = detail.get_data(as_text=True)
    assert "Entwurf" in page
    assert "Ausstellen" in page

    with app.extensions["db_session_factory"]() as db:
        invoice = db.execute(select(SalesInvoice)).scalar_one()
        invoice_id = invoice.id
        assert [line.net_amount for line in invoice.lines] == [
            Decimal("250.00"),
            Decimal("1000.50"),
        ]
        assert invoice.lines[0].unit_code == "HUR"

    # Ändern: nur noch eine Position.
    updated = client.post(
        f"/rechnungen/{invoice_id}",
        data={
            "partner_id": ids["customer"],
            "issue_date": "2026-03-15",
            "service_date": "2026-03-10",
            "tax_treatment": "standard",
            "einvoice_syntax": "cii",
            "line_description": ["Beratung"],
            "line_quantity": ["2"],
            "line_unit_code": ["C62"],
            "line_unit_price": ["100"],
            "line_tax_code_id": [ids["tax19"]],
            "line_revenue_account_id": [ids["revenue"]],
        },
    )
    assert updated.status_code == 302
    with app.extensions["db_session_factory"]() as db:
        invoice = db.get(SalesInvoice, invoice_id)
        assert invoice.gross_total == Decimal("238.00")
        assert invoice.einvoice_syntax == "cii"

    # Fehlerhafte Eingabe landet als Meldung im Formular.
    bad = client.post(
        f"/rechnungen/{invoice_id}",
        data={
            "partner_id": ids["customer"],
            "line_description": ["Beratung"],
            "line_quantity": ["abc"],
            "line_unit_price": ["1"],
        },
        follow_redirects=True,
    )
    assert "Dezimalzahl" in bad.get_data(as_text=True)

    issued = client.post(f"/rechnungen/{invoice_id}/ausstellen", follow_redirects=True)
    assert issued.status_code == 200
    page = issued.get_data(as_text=True)
    assert "RE-2026-0001" in page
    assert "Stornieren" in page
    assert "Ausgestellt" in page
    pdf = client.get(f"/rechnungen/{invoice_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.data.startswith(b"%PDF-")
    xml = client.get(f"/rechnungen/{invoice_id}/xml")
    assert parse_einvoice(xml.data).syntax == "CII"

    # Serverseitige Sortierung wie bei den anderen paginierten Listen.
    sorted_page = client.get(
        f"/rechnungen?company_id={ids['company']}&sort=customer&dir=asc"
    )
    assert sorted_page.status_code == 200
    assert 'data-sort="customer" data-sort-dir="asc"' in sorted_page.get_data(as_text=True)
    assert "RE-2026-0001" in sorted_page.get_data(as_text=True)

    # Liste mit Filtern, Partnerseite verlinkt die Rechnungsanlage.
    filtered = client.get(
        f"/rechnungen?company_id={ids['company']}&status=issued&partner_id={ids['customer']}"
    ).get_data(as_text=True)
    assert "RE-2026-0001" in filtered
    assert "RE-2026-0001" not in client.get(
        f"/rechnungen?company_id={ids['company']}&status=draft"
    ).get_data(as_text=True)
    partner_page = client.get(f"/partner/{ids['customer']}").get_data(as_text=True)
    assert f"/rechnungen/neu?company_id={ids['company']}&amp;partner_id={ids['customer']}" in (
        partner_page
    )

    cancelled = client.post(
        f"/rechnungen/{invoice_id}/stornieren",
        data={"reason": "Fehler"},
        follow_redirects=True,
    )
    assert cancelled.status_code == 200
    assert "RE-2026-0002" in cancelled.get_data(as_text=True)
    assert "Stornorechnung" in cancelled.get_data(as_text=True)


def test_ui_shows_unit_price_with_all_decimals(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        books = _Books(db)
        invoice_id = books.issued(
            tmp_path / "app-uploads", lines=[books.line(quantity="3", unit_price="33.3333")]
        ).id
    assert "33,3333" in client.get(f"/rechnungen/{invoice_id}").get_data(as_text=True)


def test_ui_draft_delete_and_problems(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        books = _Books(db)
        ids = _ids(books)
        draft = books.draft(service_date=None)
        invoice_id = draft.id
    page = client.get(f"/rechnungen/{invoice_id}").get_data(as_text=True)
    assert "Leistungsdatum oder Leistungszeitraum fehlt" in page
    issued = client.post(f"/rechnungen/{invoice_id}/ausstellen", follow_redirects=True)
    assert "Leistungsdatum oder Leistungszeitraum fehlt" in issued.get_data(as_text=True)
    deleted = client.post(f"/rechnungen/{invoice_id}/loeschen", follow_redirects=True)
    assert deleted.status_code == 200
    assert client.get(f"/rechnungen/{invoice_id}").status_code == 404
    assert client.post(
        "/rechnungen", data={"company_id": ids["company"]}, follow_redirects=True
    ).status_code == 200


def test_ui_master_data_page_and_dunning_sender(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        books = _Books(db)
        company_id = books.company.id
        issued = books.issued(tmp_path / "app-uploads")
        item_id = issued.open_item_id

    page = client.get(f"/verwaltung/firmendaten?company_id={company_id}")
    assert page.status_code == 200
    text_page = page.get_data(as_text=True)
    assert "Firmendaten" in text_page
    assert "DE123456789" in text_page
    # Die IBAN wird angezeigt, ist hier aber kein Eingabefeld.
    assert 'name="iban"' not in text_page

    saved = client.post(
        "/verwaltung/firmendaten",
        data={
            "company_id": company_id,
            "street": "Neue Straße 5",
            "postal_code": "14467",
            "city": "Potsdam",
            "country_code": "DE",
            "vat_id": "DE123456789",
            "tax_number": "30/123/45678",
            "phone": "",
            "email": "neu@example.org",
            "website": "",
            "register_court": "",
            "register_number": "",
            "managing_directors": "",
            "bank_name": "",
            "invoice_prefix": "AR",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    assert "Neue Straße 5" in saved.get_data(as_text=True)
    with app.extensions["db_session_factory"]() as db:
        company = db.get(Company, company_id)
        assert company.city == "Potsdam"
        assert company.invoice_prefix == "AR"
        assert company.is_small_business is False

    bad = client.post(
        "/verwaltung/firmendaten",
        data={"company_id": company_id, "email": "kaputt"},
        follow_redirects=True,
    )
    assert "E-Mail" in bad.get_data(as_text=True)

    letter = client.get(f"/mahnwesen/{item_id}/schreiben").get_data(as_text=True)
    assert "Neue Straße 5" in letter
    assert "14467 Potsdam" in letter


def test_ui_is_read_only_for_pruefer(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with app.extensions["db_session_factory"]() as db:
        books = _Books(db)
        ids = _ids(books)
        draft_id = books.draft().id
        db.add(
            User(
                username="pruefer",
                password_hash=hash_password("geheim123"),
                role="Pruefer",
                tenant_id=books.company.tenant_id,
            )
        )
        db.commit()
    client = app.test_client()
    client.post("/auth/login", data={"username": "pruefer", "password": "geheim123"})
    assert client.get(f"/rechnungen?company_id={ids['company']}").status_code == 200
    assert client.get(f"/rechnungen/{draft_id}").status_code == 200
    # Schreibaktionen leiten mit Hinweis um und bewirken nichts.
    denied = client.post(f"/rechnungen/{draft_id}/ausstellen", follow_redirects=True)
    assert "nur Lesezugriff" in denied.get_data(as_text=True)
    client.post(f"/rechnungen/{draft_id}/loeschen")
    client.post("/verwaltung/firmendaten", data={"company_id": ids["company"], "city": "X"})
    with app.extensions["db_session_factory"]() as db:
        draft = db.get(SalesInvoice, draft_id)
        assert draft is not None
        assert draft.status == "draft"
        assert db.get(Company, ids["company"]).city == "Berlin"


def test_einvoice_form_uses_master_data(tmp_path: Path) -> None:
    app = _app(tmp_path, SELLER_VAT_ID="DE999999999")
    client = _admin_client(app)
    with app.extensions["db_session_factory"]() as db:
        company_id = _Books(db).company.id
    response = client.post(
        "/api/v1/einvoices/export",
        json={
            "company_id": company_id,
            "invoice_number": "F-1",
            "issue_date": "2026-03-15",
            "buyer_name": "Kunde",
            "lines": [{"name": "X", "quantity": "1", "unit_price": "10", "tax_rate": "19"}],
        },
    )
    assert response.status_code == 201
    xml = response.get_json()["xml"]
    assert "Hauptstraße 1" in xml
    assert "DE123456789" in xml
    assert "DE999999999" not in xml


def test_audit_export_contains_sales_invoices(tmp_path: Path, session: Session) -> None:
    import json
    import zipfile

    from app.services.audit_export import build_audit_export_package

    books = _Books(session)
    upload_dir = tmp_path / "uploads"
    issued = books.issued(upload_dir, lines=[books.line(quantity="1.5", unit_price="33.3333")])
    books.draft()
    package = build_audit_export_package(
        session=session,
        company_id=books.company.id,
        generated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    with zipfile.ZipFile(BytesIO(package.payload)) as archive:
        invoices = json.loads(archive.read("data/sales_invoices.json"))
        lines = json.loads(archive.read("data/sales_invoice_lines.json"))
        catalog = json.loads(archive.read("schema/field_catalog.json"))
    assert [row["invoice_number"] for row in invoices] == [issued.invoice_number]
    assert len(lines) == 1
    assert lines[0]["sales_invoice_id"] == issued.id
    # Mengen und Preise behalten ihre Genauigkeit, Beträge zwei Stellen.
    assert lines[0]["quantity"] == "1.5000"
    assert lines[0]["unit_price"] == "33.3333"
    assert lines[0]["net_amount"] == "50.00"
    assert "sales_invoices" in catalog["tables"]
    assert "sales_invoice_lines" in catalog["tables"]


def test_mcp_tools_are_registered_and_route_to_api() -> None:
    from app.services.chat import CHAT_BLOCKED_TOOL_NAMES
    from app.services.mcp_server import TOOLS_BY_NAME
    from app.services.tool_titles import tool_title

    for name, method, path in (
        ("list_sales_invoices", "GET", "/sales-invoices"),
        ("get_sales_invoice", "GET", "/sales-invoices/{invoice_id}"),
        ("create_sales_invoice", "POST", "/sales-invoices"),
        ("update_sales_invoice", "PATCH", "/sales-invoices/{invoice_id}"),
        ("delete_sales_invoice", "POST", "/sales-invoices/{invoice_id}/delete"),
        ("issue_sales_invoice", "POST", "/sales-invoices/{invoice_id}/issue"),
        ("cancel_sales_invoice", "POST", "/sales-invoices/{invoice_id}/cancel"),
        ("download_sales_invoice", "GET", "/sales-invoices/{invoice_id}/content"),
        ("get_company_master_data", "GET", "/companies/{company_id}/master-data"),
        ("update_company_master_data", "PATCH", "/companies/{company_id}/master-data"),
    ):
        spec = TOOLS_BY_NAME[name]
        assert (spec.http_method, spec.path) == (method, path)
        assert tool_title(name) != name
        assert name not in CHAT_BLOCKED_TOOL_NAMES
    # Die IBAN bleibt beim gesperrten Weg.
    schema = TOOLS_BY_NAME["update_company_master_data"].input_schema
    assert "iban" not in schema["properties"]
    assert "set_company_bank_details" in CHAT_BLOCKED_TOOL_NAMES


def test_migration_roundtrip(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'roundtrip.db'}"
    session_factory = create_session_factory(url)
    with session_factory() as db:
        books = _Books(db)
        books.issued(tmp_path / "uploads")

    engine = create_engine(url)
    config = _alembic_config(engine)
    assert "sales_invoice" in inspect(engine).get_table_names()

    # Downgrade entfernt Trigger, Tabellen und Spalten.
    command.downgrade(config, "20261103_0057")
    tables = inspect(engine).get_table_names()
    assert "sales_invoice" not in tables
    assert "sales_invoice_line" not in tables
    assert "sales_invoice_sequence" not in tables
    company_columns = {column["name"] for column in inspect(engine).get_columns("company")}
    assert "invoice_prefix" not in company_columns
    assert "vat_id" not in company_columns
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    assert not {name for name in triggers if "sales_invoice" in name}

    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == _alembic_head_revision()
        triggers = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='trigger'")
            )
        }
    assert "obk_sales_invoice_issued_no_update" in triggers
    company_columns = {column["name"] for column in inspect(engine).get_columns("company")}
    assert {"invoice_prefix", "is_small_business", "vat_id"} <= company_columns
    with sessionmaker(engine)() as db:
        company = db.execute(select(Company)).scalar_one()
        assert company.invoice_prefix == "RE"
        assert company.is_small_business is False
        assert db.execute(select(SalesInvoiceLine)).scalars().first() is None
