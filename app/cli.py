from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import click
from flask import Flask
from sqlalchemy import select

from app.auth import ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, hash_password
from app.services.account_chart_import import import_account_chart_file
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import Account, Company, JournalEntry, TaxCode, Tenant, User

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CHART_FILES = {
    "skr03": PROJECT_ROOT / "data" / "kontenrahmen" / "skr03.csv",
    "skr04": PROJECT_ROOT / "data" / "kontenrahmen" / "skr04.csv",
}

DEMO_TENANT_NAME = "Demo Mandant"
DEMO_COMPANY_NAME = "Demo GmbH"
DEMO_USERS = (
    # (username, password, role, tenant-gebunden)
    ("admin", "admin123", ROLE_ADMIN, False),
    ("buchhalter", "buchhalter123", ROLE_BUCHHALTER, True),
    ("pruefer", "pruefer123", ROLE_PRUEFER, True),
)


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("import-kontenrahmen")
    @click.option("--company-id", required=True, type=int, help="ID der Zielgesellschaft")
    @click.option(
        "--csv-path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Pfad zur CSV (optional, alternativ zu --chart)",
    )
    @click.option(
        "--chart",
        type=click.Choice(tuple(BUNDLED_CHART_FILES.keys())),
        help="Vorgebundener Kontenrahmen (skr03 oder skr04)",
    )
    def import_kontenrahmen(company_id: int, csv_path: Path | None, chart: str | None):
        """Importiert SKR03/SKR04-Konten aus einer CSV-Datei."""
        if (csv_path is None and chart is None) or (csv_path is not None and chart is not None):
            raise click.BadParameter("Bitte genau eine Option angeben: --csv-path ODER --chart")

        resolved_csv_path = csv_path or BUNDLED_CHART_FILES[chart]
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            report = import_account_chart_file(
                session=session,
                company_id=company_id,
                csv_path=resolved_csv_path,
            )

        click.echo(
            "Import abgeschlossen: "
            f"gesamt={report.total_rows}, "
            f"importiert={report.imported_rows}, "
            f"duplikate={report.duplicate_rows}, "
            f"fehler={report.error_rows}"
        )
        for error in report.errors:
            click.echo(f"- Zeile {error.line_number}: {error.message}")

    @app.cli.command("create-user")
    @click.option("--username", required=True)
    @click.option("--password", required=True)
    @click.option(
        "--role",
        type=click.Choice([ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER]),
        default=ROLE_BUCHHALTER,
        show_default=True,
    )
    @click.option("--tenant-id", type=int, default=None, help="Leer = globaler Zugriff")
    def create_user(username: str, password: str, role: str, tenant_id: int | None):
        """Legt einen Benutzer für den Web-Login an."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            existing = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if existing is not None:
                raise click.ClickException(f"Benutzer {username} existiert bereits.")

            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                    tenant_id=tenant_id,
                )
            )
            session.commit()
        click.echo(f"Benutzer {username} ({role}) wurde angelegt.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Legt Demo-Mandant, SKR03-Konten, Steuercodes, Benutzer und Buchungen an (idempotent)."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            tenant = session.execute(
                select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
            ).scalar_one_or_none()
            if tenant is None:
                tenant = Tenant(name=DEMO_TENANT_NAME)
                session.add(tenant)
                session.flush()
                click.echo(f"Mandant '{DEMO_TENANT_NAME}' angelegt.")

            company = session.execute(
                select(Company).where(
                    Company.tenant_id == tenant.id, Company.name == DEMO_COMPANY_NAME
                )
            ).scalar_one_or_none()
            if company is None:
                company = Company(name=DEMO_COMPANY_NAME, currency_code="EUR", tenant=tenant)
                session.add(company)
                session.flush()
                click.echo(f"Gesellschaft '{DEMO_COMPANY_NAME}' angelegt.")
            session.commit()

            report = import_account_chart_file(
                session=session,
                company_id=company.id,
                csv_path=BUNDLED_CHART_FILES["skr03"],
            )
            click.echo(
                f"SKR03: {report.imported_rows} Konten importiert, "
                f"{report.duplicate_rows} übersprungen."
            )

            created_tax_codes = ensure_default_tax_codes(session=session, company=company)
            session.commit()
            click.echo(f"Steuercodes: {created_tax_codes} neu angelegt.")

            for username, password, role, tenant_bound in DEMO_USERS:
                existing = session.execute(
                    select(User).where(User.username == username)
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(
                    User(
                        username=username,
                        password_hash=hash_password(password),
                        role=role,
                        tenant_id=tenant.id if tenant_bound else None,
                    )
                )
                click.echo(f"Benutzer '{username}' ({role}) angelegt, Passwort: {password}")
            session.commit()

            has_entries = (
                session.execute(
                    select(JournalEntry.id).where(JournalEntry.company_id == company.id)
                ).first()
                is not None
            )
            if has_entries:
                click.echo("Beispielbuchungen übersprungen (Buchungen vorhanden).")
                return

            def account_id(code: str) -> int:
                account = session.execute(
                    select(Account).where(Account.company_id == company.id, Account.code == code)
                ).scalar_one()
                return account.id

            def tax_code_id(code: str) -> int:
                tax_code = session.execute(
                    select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == code)
                ).scalar_one()
                return tax_code.id

            today = date.today()
            zero = Decimal("0.00")
            demo_entries = [
                JournalEntryInput(
                    company_id=company.id,
                    entry_date=today.replace(day=1),
                    description="Privateinlage Startkapital",
                    status="posted",
                    changed_by="seed-demo",
                    lines=[
                        JournalLineInput(account_id("1200"), Decimal("25000.00"), zero),
                        JournalLineInput(account_id("1890"), zero, Decimal("25000.00")),
                    ],
                ),
                JournalEntryInput(
                    company_id=company.id,
                    entry_date=today.replace(day=2),
                    description="Ausgangsrechnung Beratung (netto 1.000 € + 19 % USt)",
                    status="posted",
                    changed_by="seed-demo",
                    lines=[
                        JournalLineInput(account_id("1400"), Decimal("1190.00"), zero),
                        JournalLineInput(
                            account_id("8400"),
                            zero,
                            Decimal("1000.00"),
                            tax_code_id=tax_code_id("USt19"),
                        ),
                    ],
                ),
                JournalEntryInput(
                    company_id=company.id,
                    entry_date=today.replace(day=3),
                    description="Eingangsrechnung Büromiete (netto 500 € + 19 % VSt)",
                    status="posted",
                    changed_by="seed-demo",
                    lines=[
                        JournalLineInput(
                            account_id("4200"),
                            Decimal("500.00"),
                            zero,
                            tax_code_id=tax_code_id("VSt19"),
                        ),
                        JournalLineInput(account_id("1600"), zero, Decimal("595.00")),
                    ],
                ),
                JournalEntryInput(
                    company_id=company.id,
                    entry_date=today.replace(day=4),
                    description="Zahlungseingang Ausgangsrechnung",
                    status="posted",
                    changed_by="seed-demo",
                    lines=[
                        JournalLineInput(account_id("1200"), Decimal("1190.00"), zero),
                        JournalLineInput(account_id("1400"), zero, Decimal("1190.00")),
                    ],
                ),
            ]
            for entry_input in demo_entries:
                entry = create_journal_entry(session=session, payload=entry_input)
                click.echo(f"Buchung {entry.posting_number}: {entry.description}")

        click.echo("Demo-Daten sind bereit. Login z. B. mit buchhalter/buchhalter123.")
