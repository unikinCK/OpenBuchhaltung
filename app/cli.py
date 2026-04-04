from __future__ import annotations

import click
from flask import Flask

from app.services.account_chart_import import import_account_chart_file


def register_cli_commands(app: Flask) -> None:
    @app.cli.command("import-kontenrahmen")
    @click.option("--company-id", required=True, type=int, help="ID der Zielgesellschaft")
    @click.option("--csv-path", required=True, type=click.Path(exists=True), help="Pfad zur CSV")
    def import_kontenrahmen(company_id: int, csv_path: str):
        """Importiert SKR03/SKR04-Konten aus einer CSV-Datei."""
        session_factory = app.extensions["db_session_factory"]
        with session_factory() as session:
            report = import_account_chart_file(
                session=session,
                company_id=company_id,
                csv_path=csv_path,
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
