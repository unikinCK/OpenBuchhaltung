from __future__ import annotations

import logging
from pathlib import Path

import click
from flask import Flask
from sqlalchemy import select

from app.auth import (
    ROLE_ADMIN,
    ROLE_BUCHHALTER,
    ROLE_PRUEFER,
    ROLE_SUPPORT,
    generate_api_token,
    hash_api_token,
    hash_password,
    password_policy_error,
)
from app.services.account_chart_import import import_account_chart_file
from app.services.audit_export import verify_audit_export_package
from app.services.audit_log import verify_audit_log_integrity
from app.services.compliance_integrity import verify_compliance_integrity
from app.services.demo import (
    DEMO_USERS,
    PUBLIC_DEMO_USERS,
    reset_demo_database,
    seed_demo_activity,
    seed_demo_data,
)
from app.services.fints_institutes import FinTSInstituteError, import_fints_institutes
from app.services.security_events import record_security_event
from domain.models import (
    User,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CHART_FILES = {
    "skr03": PROJECT_ROOT / "data" / "kontenrahmen" / "skr03.csv",
    "skr04": PROJECT_ROOT / "data" / "kontenrahmen" / "skr04.csv",
}

CLI_ACTOR = "cli"


def _is_production_app(app: Flask) -> bool:
    # Lokaler Import: app/__init__ importiert dieses Modul (Zirkularität vermeiden).
    from app import is_production

    return is_production(app)


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("verify-audit-package")
    @click.argument(
        "package_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    def verify_audit_package(package_path: Path):
        """Prüft Dateihashes und Gesamtnachweis eines Prüferexport-ZIPs."""
        result = verify_audit_export_package(package_path.read_bytes())
        click.echo(f"Paket-SHA-256: {result.package_sha256}")
        click.echo(
            f"Geprüft: {result.files_checked} Dateien; "
            f"Datenbestand-SHA-256: {result.dataset_sha256 or '-'}"
        )
        if result.valid:
            click.echo("Prüferexport ist intakt.")
            return
        for issue in result.issues:
            click.echo(f"{issue.path or '-'}: {issue.code} – {issue.message}")
        raise click.ClickException("Prüferexport ist nicht intakt.")

    @app.cli.command("verify-integrity")
    @click.option("--tenant-id", type=int, default=None, help="Optionaler einzelner Mandant")
    @click.option("--company-id", type=int, default=None, help="Optionale Gesellschaft")
    def verify_integrity(tenant_id: int | None, company_id: int | None):
        """Prüft Buchungen, Belege und Audit-Hashketten kryptografisch."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            try:
                result = verify_compliance_integrity(
                    session=session,
                    tenant_id=tenant_id,
                    company_id=company_id,
                )
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc

        click.echo(
            f"Geprüft: {result.finalized_entries_checked} festgeschriebene Buchungen, "
            f"{result.documents_checked} Belege, "
            f"{result.audit.checked_entries} Audit-Einträge."
        )
        if result.valid:
            click.echo("Integritätsnachweise sind intakt.")
            return

        for issue in result.issues:
            click.echo(f"{issue.area} {issue.entity_id or '-'}: {issue.code} – {issue.message}")
        for tenant in result.audit.tenants:
            for issue in tenant.issues:
                click.echo(
                    f"Mandant {tenant.tenant_id}, Sequenz "
                    f"{issue.sequence_number or '-'}: {issue.code} – {issue.message}"
                )
        raise click.ClickException("Integritätsnachweise sind nicht intakt.")

    @app.cli.command("verify-audit-log")
    @click.option("--tenant-id", type=int, default=None, help="Optionaler einzelner Mandant")
    def verify_audit_log(tenant_id: int | None):
        """Prüft die kryptografische Integrität der Audit-Hashketten."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            result = verify_audit_log_integrity(session=session, tenant_id=tenant_id)

        if result.valid:
            click.echo(
                "Audit-Hashkette intakt: "
                f"{result.checked_entries} Einträge in "
                f"{result.checked_tenants} Mandanten geprüft."
            )
            return

        for tenant in result.tenants:
            for issue in tenant.issues:
                click.echo(
                    f"Mandant {tenant.tenant_id}, Sequenz "
                    f"{issue.sequence_number or '-'}: {issue.code} – {issue.message}"
                )
        raise click.ClickException("Audit-Hashkette ist nicht intakt.")

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

    @app.cli.command("import-fints-institute")
    @click.option(
        "--csv-path",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Pfad zur CSV der FinTS-Bankenliste (Deutsche Kreditwirtschaft)",
    )
    def import_fints_institute(csv_path: Path):
        """Ersetzt die FinTS-Bankenliste (BLZ → PIN/TAN-URL, BIC)."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            try:
                report = import_fints_institutes(
                    session=session, content=csv_path.read_bytes(), changed_by="cli"
                )
            except FinTSInstituteError as exc:
                raise click.ClickException(str(exc)) from exc

        click.echo(
            "FinTS-Bankenliste importiert: "
            f"einträge={report.imported_rows}, blz={report.distinct_blz}, "
            f"mit_url={report.with_url}, übersprungen={report.skipped_rows}, "
            f"duplikate={report.duplicate_rows}"
        )
        for error in report.errors:
            click.echo(f"- {error}")

    @app.cli.command("create-user")
    @click.option("--username", required=True)
    @click.option(
        "--password",
        prompt="Passwort",
        hide_input=True,
        confirmation_prompt=True,
        help="Wird interaktiv abgefragt, wenn nicht angegeben (empfohlen: nicht als "
        "Option übergeben, sonst landet es in der Shell-History).",
    )
    @click.option(
        "--role",
        type=click.Choice([ROLE_ADMIN, ROLE_BUCHHALTER, ROLE_PRUEFER, ROLE_SUPPORT]),
        default=ROLE_BUCHHALTER,
        show_default=True,
    )
    @click.option("--tenant-id", type=int, default=None, help="Leer = globaler Zugriff")
    def create_user(username: str, password: str, role: str, tenant_id: int | None):
        """Legt einen Benutzer für den Web-Login an."""
        policy_error = password_policy_error(password)
        if policy_error:
            raise click.ClickException(policy_error)
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            existing = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if existing is not None:
                raise click.ClickException(f"Benutzer {username} existiert bereits.")

            user = User(
                username=username,
                password_hash=hash_password(password),
                role=role,
                tenant_id=tenant_id,
            )
            session.add(user)
            session.flush()
            record_security_event(
                session,
                user=user,
                action="created",
                actor=CLI_ACTOR,
                payload={"username": username, "role": role, "tenant_id": tenant_id},
            )
            session.commit()
        click.echo(f"Benutzer {username} ({role}) wurde angelegt.")

    @app.cli.command("set-password")
    @click.option("--username", required=True)
    @click.option(
        "--password",
        prompt="Neues Passwort",
        hide_input=True,
        confirmation_prompt=True,
        help="Wird interaktiv abgefragt, wenn nicht angegeben.",
    )
    def set_password(username: str, password: str):
        """Setzt das Passwort eines Benutzers neu (z. B. zur Wiederherstellung)."""
        policy_error = password_policy_error(password)
        if policy_error:
            raise click.ClickException(policy_error)
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            user = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if user is None:
                raise click.ClickException(f"Benutzer {username} wurde nicht gefunden.")
            user.password_hash = hash_password(password)
            record_security_event(session, user=user, action="password_reset", actor=CLI_ACTOR)
            session.commit()
        click.echo(f"Passwort für {username} wurde gesetzt.")

    @app.cli.command("set-api-token")
    @click.option("--username", required=True)
    def set_api_token(username: str):
        """Erzeugt/rotiert ein API-Token für einen Benutzer und zeigt es einmalig an."""
        token = generate_api_token()
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            user = session.execute(
                select(User).where(User.username == username)
            ).scalar_one_or_none()
            if user is None:
                raise click.ClickException(f"Benutzer {username} wurde nicht gefunden.")
            user.api_token_hash = hash_api_token(token)
            user.api_token_last4 = token[-4:]
            record_security_event(
                session,
                user=user,
                action="api_token_rotated",
                actor=CLI_ACTOR,
                payload={"api_token_last4": token[-4:]},
            )
            session.commit()

        click.echo(f"API-Token für {username}: {token}")
        click.echo("Dieses Token wird nur einmal angezeigt.")

    @app.cli.command("push-dispatch")
    @click.option("--once", is_flag=True, help="Nur einen Durchlauf statt Dauerbetrieb.")
    def push_dispatch(once: bool):
        """Stellt Push-Mitteilungen über das Push-Relay zu (Dauerbetrieb im Compose-Dienst push)."""
        import time

        from app.services.push import push_enabled, run_dispatch_cycle

        if not push_enabled(app.config):
            raise click.ClickException("PUSH_RELAY_URL ist nicht gesetzt.")
        interval = max(5, int(app.config.get("PUSH_DISPATCH_INTERVAL_SECONDS") or 30))
        session_factory = app.extensions["db_session_factory"]
        while True:
            with session_factory() as session:
                try:
                    counts = run_dispatch_cycle(session, config=app.config)
                except Exception:  # Dauerbetrieb: Fehler protokollieren, weiterlaufen
                    session.rollback()
                    logging.getLogger(__name__).exception("Push-Dispatch fehlgeschlagen")
                    counts = None
            if counts and any(counts.values()):
                click.echo(
                    "push-dispatch: "
                    + ", ".join(f"{key}={value}" for key, value in counts.items())
                )
            if once:
                return
            time.sleep(interval)

    @app.cli.command("seed-demo")
    def seed_demo():
        """Legt Demo-Mandant, SKR03-Konten, Steuercodes, Benutzer und Buchungen an (idempotent)."""
        if _is_production_app(app) and not app.config.get("DEMO_MODE"):
            raise click.ClickException(
                "seed-demo legt Demo-Benutzer mit bekannten Passwörtern an und ist in "
                "Produktion (APP_ENV ist keine Entwicklungsumgebung) nicht erlaubt."
            )
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            company = seed_demo_data(
                session,
                users=PUBLIC_DEMO_USERS if app.config.get("DEMO_MODE") else DEMO_USERS,
                echo=click.echo,
            )
            seed_demo_activity(
                session,
                company,
                upload_dir=Path(app.config["DOCUMENT_UPLOAD_DIR"]),
                echo=click.echo,
            )
        click.echo("Demo-Daten sind bereit. Login z. B. mit buchhalter/buchhalter123.")

    @app.cli.command("demo-reset")
    @click.option("--loop", is_flag=True, help="Dauerbetrieb: täglich zu DEMO_RESET_TIME.")
    def demo_reset(loop: bool):
        """Leert die Datenbank vollständig und spielt die Demo-Daten neu ein."""
        if not app.config.get("DEMO_MODE") and _is_production_app(app):
            raise click.ClickException(
                "demo-reset löscht alle Daten und ist nur mit DEMO_MODE=1 oder in einer "
                "Entwicklungsumgebung erlaubt."
            )
        import time
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        session_factory = app.extensions["db_session_factory"]
        upload_dir = Path(app.config["DOCUMENT_UPLOAD_DIR"])
        public = bool(app.config.get("DEMO_MODE"))

        def run_reset() -> None:
            reset_demo_database(session_factory, upload_dir=upload_dir, public=public,
                                echo=click.echo)

        if not loop:
            run_reset()
            return
        zone = ZoneInfo("Europe/Berlin")
        hour, minute = (int(part) for part in str(app.config["DEMO_RESET_TIME"]).split(":"))
        while True:
            now = datetime.now(zone)
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
            click.echo(f"Nächster Demo-Reset: {due:%d.%m.%Y %H:%M}")
            time.sleep((due - now).total_seconds())
            try:
                run_reset()
            except Exception:  # Dauerbetrieb: Fehler protokollieren, morgen erneut
                logging.getLogger(__name__).exception("Demo-Reset fehlgeschlagen")
