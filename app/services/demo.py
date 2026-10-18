"""Demo-Daten und öffentliche Demo-Instanz (``DEMO_MODE``).

* :func:`seed_demo_data` legt Mandant, Gesellschaft (SKR03), Steuercodes, Partner,
  Benutzer und Beispielbuchungen an (``flask seed-demo``, idempotent).
* :func:`seed_demo_activity` ergänzt Alltag für App und Weboberfläche: gebuchte und
  offene Bankumsätze (mit Vorschlägen „wie zuletzt“ und per Regel), einen
  überfälligen offenen Posten und einen Beleg ohne Buchung.
* :func:`reset_demo_database` leert die Datenbank vollständig und spielt die
  Demo-Daten neu ein (``flask demo-reset``). PostgreSQL wird per ``TRUNCATE``
  geleert: Die GoBD-Trigger verhindern einzelne Löschungen (zu Recht), und das
  Schema bleibt erhalten, sodass laufende App-Prozesse weiterarbeiten.
* :func:`demo_block_reason` sperrt in der Demo-Instanz alles, was Geheimnisse
  speichert oder nach außen wirkt (Benutzerverwaltung, KI-Schlüssel, FinTS, Open
  Banking, ELSTER-Übermittlung).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.auth import ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, ROLE_SUPPORT, hash_password
from app.services.account_chart_import import import_account_chart_file
from app.services.bank_import import book_transaction, import_bank_csv
from app.services.bank_rules import create_rule
from app.services.document_categories import ensure_default_document_categories
from app.services.documents import document_file_metadata
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.open_items import OpenItemInput, create_open_item
from app.services.partners import create_partner
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    BankTransaction,
    BusinessPartner,
    Company,
    Document,
    JournalEntry,
    TaxCode,
    Tenant,
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKR03_CHART = PROJECT_ROOT / "data" / "kontenrahmen" / "skr03.csv"

DEMO_TENANT_NAME = "Demo Mandant"
DEMO_COMPANY_NAME = "Demo GmbH"
DEMO_ACTOR = "seed-demo"
# Entwicklung: (Benutzername, Passwort, Rolle, mandantengebunden)
DEMO_USERS = (
    ("admin", "admin123", ROLE_ADMIN, False),
    ("buchhalter", "buchhalter123", ROLE_BUCHHALTER, True),
    ("pruefer", "pruefer123", ROLE_PRUEFER, True),
    ("support", "support123", ROLE_SUPPORT, False),
)
# Öffentliche Demo-Instanz: nur ein mandantengebundener Buchhalter, Zugangsdaten
# stehen auf der Login-Seite und in den Store-Angaben der App.
DEMO_LOGIN_USERNAME = "demo"
DEMO_LOGIN_PASSWORD = "demo1234"
PUBLIC_DEMO_USERS = ((DEMO_LOGIN_USERNAME, DEMO_LOGIN_PASSWORD, ROLE_BUCHHALTER, True),)
# (Name, Kunde, Lieferant, Ort, Zahlungsziel in Tagen)
DEMO_PARTNERS = (
    ("Beratungskunde Nord GmbH", True, False, "Hamburg", 14),
    ("Handelshaus Süd AG", True, False, "München", 30),
    ("Agentur West UG", True, True, "Köln", 14),
    ("Büro & Raum Vermietung KG", False, True, "Berlin", 0),
    ("IT-Service Ost GmbH", False, True, "Leipzig", 14),
    ("Stadtwerke Musterstadt", False, True, "Musterstadt", 14),
)

# Schreibzugriffe, die die Demo-Instanz ablehnt (Pfad-Präfix, Grund).
_DEMO_BLOCKED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), reason)
    for pattern, reason in (
        (r"^/api/v1/(users|tenants)(/|$)", "Benutzer- und Mandantenverwaltung"),
        (r"^/(users|tenants)(/|$)", "Benutzer- und Mandantenverwaltung"),
        (r"^/auth/password$", "Passwortänderung"),
        (r"^/api/v1/(fints-|open-banking)", "Bankverbindungen"),
        (r"^/bank/(fints|open-banking)(/|$)", "Bankverbindungen"),
        (r"^/verwaltung/fints-bankenliste$", "Bankverbindungen"),
        (r"^/api/v1/elster/.*(submit|retry)$", "ELSTER-Übermittlung"),
        (r"^/ustva/.*(elster-test|retry)$", "ELSTER-Übermittlung"),
    )
)
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def demo_block_reason(path: str, method: str) -> str | None:
    """Grund, warum die Demo-Instanz diesen Schreibzugriff ablehnt (sonst ``None``)."""
    if method.upper() not in WRITE_METHODS:
        return None
    for pattern, reason in _DEMO_BLOCKED:
        if pattern.search(path):
            return reason
    return None


# ---------------------------------------------------------------------------
# Demo-Daten
# ---------------------------------------------------------------------------


def seed_demo_data(
    session: Session,
    *,
    users=DEMO_USERS,
    echo: Callable[[str], Any] = lambda _message: None,
) -> Company:
    """Stammdaten, Benutzer und vier Beispielbuchungen (idempotent)."""
    tenant = session.execute(
        select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
    ).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name=DEMO_TENANT_NAME)
        session.add(tenant)
        session.flush()
        echo(f"Mandant '{DEMO_TENANT_NAME}' angelegt.")

    company = session.execute(
        select(Company).where(Company.tenant_id == tenant.id, Company.name == DEMO_COMPANY_NAME)
    ).scalar_one_or_none()
    if company is None:
        company = Company(name=DEMO_COMPANY_NAME, currency_code="EUR", tenant=tenant)
        session.add(company)
        session.flush()
        echo(f"Gesellschaft '{DEMO_COMPANY_NAME}' angelegt.")
    session.commit()

    report = import_account_chart_file(session=session, company_id=company.id, csv_path=SKR03_CHART)
    echo(f"SKR03: {report.imported_rows} Konten importiert, {report.duplicate_rows} übersprungen.")

    created_tax_codes = ensure_default_tax_codes(session=session, company=company)
    session.commit()
    echo(f"Steuercodes: {created_tax_codes} neu angelegt.")

    created_categories = ensure_default_document_categories(session=session, company=company)
    session.commit()
    echo(f"Belegkategorien: {created_categories} neu angelegt.")

    has_partners = (
        session.execute(
            select(BusinessPartner.id).where(BusinessPartner.company_id == company.id)
        ).first()
        is not None
    )
    if not has_partners:
        for name, is_customer, is_supplier, city, term in DEMO_PARTNERS:
            create_partner(
                session=session,
                company=company,
                changed_by=DEMO_ACTOR,
                name=name,
                is_customer=is_customer,
                is_supplier=is_supplier,
                city=city,
                payment_term_days=term,
            )
        session.commit()
        echo(f"Geschäftspartner: {len(DEMO_PARTNERS)} angelegt.")

    for username, password, role, tenant_bound in users:
        if session.execute(select(User).where(User.username == username)).scalar_one_or_none():
            continue
        session.add(
            User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                tenant_id=tenant.id if tenant_bound else None,
            )
        )
        echo(f"Benutzer '{username}' ({role}) angelegt, Passwort: {password}")
    session.commit()

    has_entries = (
        session.execute(
            select(JournalEntry.id).where(JournalEntry.company_id == company.id)
        ).first()
        is not None
    )
    if has_entries:
        echo("Beispielbuchungen übersprungen (Buchungen vorhanden).")
        return company

    ids = _DemoIds(session, company)
    today = date.today()
    zero = Decimal("0.00")
    entries = [
        JournalEntryInput(
            company_id=company.id,
            entry_date=today.replace(day=1),
            description="Privateinlage Startkapital",
            status="posted",
            changed_by=DEMO_ACTOR,
            lines=[
                JournalLineInput(ids.account("1200"), Decimal("25000.00"), zero),
                JournalLineInput(ids.account("1890"), zero, Decimal("25000.00")),
            ],
        ),
        JournalEntryInput(
            company_id=company.id,
            entry_date=today.replace(day=2),
            description="Ausgangsrechnung Beratung (netto 1.000 € + 19 % USt)",
            status="posted",
            changed_by=DEMO_ACTOR,
            lines=[
                JournalLineInput(
                    ids.account("1400"),
                    Decimal("1190.00"),
                    zero,
                    partner_id=ids.partner("Beratungskunde Nord GmbH"),
                ),
                JournalLineInput(
                    ids.account("8400"), zero, Decimal("1000.00"), tax_code_id=ids.tax("USt19")
                ),
            ],
        ),
        JournalEntryInput(
            company_id=company.id,
            entry_date=today.replace(day=3),
            description="Eingangsrechnung Büromiete (netto 500 € + 19 % VSt)",
            status="posted",
            changed_by=DEMO_ACTOR,
            lines=[
                JournalLineInput(
                    ids.account("4200"), Decimal("500.00"), zero, tax_code_id=ids.tax("VSt19")
                ),
                JournalLineInput(
                    ids.account("1600"),
                    zero,
                    Decimal("595.00"),
                    partner_id=ids.partner("Büro & Raum Vermietung KG"),
                ),
            ],
        ),
        JournalEntryInput(
            company_id=company.id,
            entry_date=today.replace(day=4),
            description="Zahlungseingang Ausgangsrechnung",
            status="posted",
            changed_by=DEMO_ACTOR,
            lines=[
                JournalLineInput(ids.account("1200"), Decimal("1190.00"), zero),
                JournalLineInput(
                    ids.account("1400"),
                    zero,
                    Decimal("1190.00"),
                    partner_id=ids.partner("Beratungskunde Nord GmbH"),
                ),
            ],
        ),
    ]
    for entry_input in entries:
        entry = create_journal_entry(session=session, payload=entry_input)
        echo(f"Buchung {entry.posting_number}: {entry.description}")
    return company


class _DemoIds:
    """Nachschlagen von Konten, Partnern und Steuercodes der Demo-Gesellschaft."""

    def __init__(self, session: Session, company: Company) -> None:
        self.session = session
        self.company = company

    def account(self, code: str) -> int:
        return self.session.execute(
            select(Account.id).where(Account.company_id == self.company.id, Account.code == code)
        ).scalar_one()

    def partner(self, name: str) -> int:
        return self.session.execute(
            select(BusinessPartner.id).where(
                BusinessPartner.company_id == self.company.id, BusinessPartner.name == name
            )
        ).scalar_one()

    def tax(self, code: str) -> int:
        return self.session.execute(
            select(TaxCode.id).where(TaxCode.company_id == self.company.id, TaxCode.code == code)
        ).scalar_one()


def _csv_amount(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def seed_demo_activity(
    session: Session,
    company: Company,
    *,
    upload_dir: Path,
    today: date | None = None,
    echo: Callable[[str], Any] = lambda _message: None,
) -> None:
    """Alltag der Demo-Gesellschaft: Bankumsätze, Regel, offene Posten, ein Beleg.

    Bereits vorhandene Bankumsätze werden nicht erneut angelegt (idempotent).
    """
    if session.execute(
        select(BankTransaction.id).where(BankTransaction.company_id == company.id)
    ).first():
        echo("Demo-Alltag übersprungen (Bankumsätze vorhanden).")
        return
    today = today or date.today()
    ids = _DemoIds(session, company)
    bank_id = ids.account("1200")

    # (Tage zurück, Verwendungszweck, Gegenseite, Betrag)
    rows = [
        (34, "Telekom Rechnung Vormonat", "Telekom Deutschland GmbH", Decimal("-39.99")),
        (31, "Abschlag Strom", "Stadtwerke Musterstadt", Decimal("-89.00")),
        (28, "Google Ads Kampagne", "Google Ireland Ltd.", Decimal("-120.00")),
        (6, "Google Ads Kampagne", "Google Ireland Ltd.", Decimal("-150.00")),
        (4, "Telekom Rechnung", "Telekom Deutschland GmbH", Decimal("-39.99")),
        (3, "Abschlag Strom", "Stadtwerke Musterstadt", Decimal("-89.00")),
        (2, "RE-2026-012", "Handelshaus Süd AG", Decimal("2380.00")),
        (1, "Kontoführung", "", Decimal("-9.90")),
    ]
    csv_lines = ["Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag"]
    for days, purpose, counterparty, amount in rows:
        day = today - timedelta(days=days)
        csv_lines.append(f"{day:%d.%m.%Y};{purpose};{counterparty};{_csv_amount(amount)}")
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank_id,
        csv_stream=StringIO("\n".join(csv_lines) + "\n"),
        changed_by=DEMO_ACTOR,
    )

    transactions = session.execute(
        select(BankTransaction)
        .where(BankTransaction.company_id == company.id)
        .order_by(BankTransaction.booking_date, BankTransaction.id)
    ).scalars().all()
    # Vormonat gebucht: liefert „wie zuletzt“ für die aktuellen Umsätze.
    history = {
        "Telekom Rechnung Vormonat": ("4900", "VSt19"),
        "Abschlag Strom": ("4300", "VSt19"),
    }
    booked_purposes: set[str] = set()
    for transaction in transactions:
        target = history.get(transaction.purpose)
        if target is None or transaction.purpose in booked_purposes:
            continue
        book_transaction(
            session=session,
            transaction_id=transaction.id,
            contra_account_id=ids.account(target[0]),
            tax_code_id=ids.tax(target[1]),
            changed_by=DEMO_ACTOR,
        )
        booked_purposes.add(transaction.purpose)

    create_rule(
        session=session,
        company_id=company.id,
        pattern="Google Ads",
        contra_account_id=ids.account("4600"),
        tax_code_id=ids.tax("VSt19"),
        changed_by=DEMO_ACTOR,
    )

    for reference, partner, amount, entry_days, due_days in (
        ("RE-2026-009", "Agentur West UG", Decimal("595.00"), 40, 26),
        ("RE-2026-012", "Handelshaus Süd AG", Decimal("2380.00"), 20, -10),
    ):
        create_open_item(
            session=session,
            payload=OpenItemInput(
                company_id=company.id,
                account_id=ids.account("1400"),
                item_type="receivable",
                reference=reference,
                counterparty=partner,
                entry_date=today - timedelta(days=entry_days),
                due_date=today - timedelta(days=due_days),
                amount=amount,
                changed_by=DEMO_ACTOR,
            ),
        )

    pdf = _demo_invoice_pdf(today - timedelta(days=5))
    target_dir = upload_dir / str(company.tenant_id) / str(company.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "demo-buerobedarf.pdf"
    target.write_bytes(pdf)
    metadata = document_file_metadata(pdf)
    session.add(
        Document(
            tenant_id=company.tenant_id,
            company_id=company.id,
            file_name="Rechnung-Buerobedarf.pdf",
            storage_key=str(target),
            mime_type="application/pdf",
            file_sha256=metadata.file_sha256,
            file_size_bytes=metadata.file_size_bytes,
            document_date=today - timedelta(days=5),
            capture_source="web",
        )
    )
    session.commit()
    echo("Demo-Alltag angelegt: Bankumsätze, Regel „Google Ads“, offene Posten, Beleg.")


def _demo_invoice_pdf(issue_date: date) -> bytes:
    """Kleine PDF-Rechnung mit Textebene (lesbar für Beleg-OCR und Texterkennung)."""
    lines = [
        "Muster Buerobedarf GmbH",
        "Rechnung Nr. MB-2026-118",
        f"Rechnungsdatum: {issue_date:%d.%m.%Y}",
        "Druckerpapier und Toner     84,03 EUR",
        "MwSt 19 %                   15,97 EUR",
        "Gesamtbetrag               100,00 EUR",
    ]
    shows = " ".join(f"({line}) Tj 0 -16 Td" for line in lines)
    content = f"BT /F1 12 Tf 50 780 Td {shows} ET".encode("latin-1")
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length " + str(len(content)).encode("ascii") + b">>\nstream\n"
        + content
        + b"\nendstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    )
    return pdf + b"%" + b"0" * max(0, 1100 - len(pdf)) + b"\n%%EOF"


# ---------------------------------------------------------------------------
# Zurücksetzen
# ---------------------------------------------------------------------------


def wipe_database(engine) -> None:
    """Leert alle Tabellen; das Schema steht danach wieder auf dem Alembic-Head.

    PostgreSQL: ``TRUNCATE … RESTART IDENTITY CASCADE`` (Schema und Trigger bleiben).
    SQLite: alle Tabellen löschen und neu migrieren (``DELETE`` scheitert an den
    Unveränderbarkeits-Triggern).
    """
    from alembic import command

    from app.db import _alembic_config  # lokal: Zirkularität app.db ↔ Services vermeiden

    command.upgrade(_alembic_config(engine), "head")
    tables = [name for name in inspect(engine).get_table_names() if name != "alembic_version"]
    if engine.dialect.name == "postgresql":
        if tables:
            quoted = ", ".join(f'"{name}"' for name in tables)
            with engine.begin() as connection:
                connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        return
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        for name in [*tables, "alembic_version"]:
            connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        connection.execute(text("PRAGMA foreign_keys=ON"))
    command.upgrade(_alembic_config(engine), "head")


def reset_demo_database(
    session_factory,
    *,
    upload_dir: Path,
    public: bool,
    echo: Callable[[str], Any] = lambda _message: None,
) -> None:
    """Datenbank und Uploads leeren, dann Demo-Daten neu einspielen."""
    engine = session_factory.kw["bind"]
    engine.dispose()
    wipe_database(engine)
    if upload_dir.exists():
        for child in upload_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    with session_factory() as session:
        company = seed_demo_data(
            session, users=PUBLIC_DEMO_USERS if public else DEMO_USERS, echo=echo
        )
        seed_demo_activity(session, company, upload_dir=upload_dir, echo=echo)
    echo("Demo-Datenbank zurückgesetzt.")
