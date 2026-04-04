from __future__ import annotations

from pathlib import Path

import click
from flask import Flask

from app.services.account_chart_import import import_account_chart_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_CHART_FILES = {
    "skr03": PROJECT_ROOT / "data" / "kontenrahmen" / "skr03.csv",
    "skr04": PROJECT_ROOT / "data" / "kontenrahmen" / "skr04.csv",
}


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
