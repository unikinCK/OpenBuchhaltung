"""Bankumsätze über den MCP-Server der Bank abrufen (z. B. Finom) – Seite „Bank-MCP-Abruf“.

Die Seite zeigt die Wallets beim Anbieter (live, mit dem Zugang des Benutzers), je
Wallet das vorgeschlagene Bank-Sachkonto und einen Zeitraum. „Vorschau“ zeigt die neuen
Umsätze, „Übernehmen“ importiert sie. Der Abruf läuft ohne KI (siehe
:mod:`app.services.mcp_bank_sync`).
"""

from __future__ import annotations

from datetime import date

from flask import flash, redirect, render_template, request, url_for

from app.auth import current_user
from app.services.mcp_bank_sync import (
    McpBankSyncError,
    ServerCaller,
    bank_account_for_iban,
    bank_sync_servers,
    default_sync_range,
    last_bank_account_id,
    list_wallets,
    resolve_server,
    sync_mcp_bank_transactions,
)
from app.services.scoping import scoped_select
from app.services.standard_accounts import BANK, standard_account_id
from app.web.blueprint import main_bp
from app.web.helpers import changed_by, get_session_factory, require_company_access
from domain.models import Account


def _date(raw: str | None, label: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise McpBankSyncError(f"Ungültiges Datum im Feld {label}.") from exc


def _bank_accounts(session, company_id: int) -> list[Account]:
    accounts = session.execute(
        scoped_select(Account, company_id=company_id)
        .where(
            Account.account_type == "asset",
            Account.subledger.is_(None),
            Account.is_active.is_(True),
        )
        .order_by(Account.code)
    ).scalars()
    return list(accounts)


def _caller(session_factory, server_id: int, company) -> ServerCaller:
    return ServerCaller(
        session_factory=session_factory,
        server_id=server_id,
        tenant_id=company.tenant_id,
        company_id=company.id,
        user=current_user(),
    )


@main_bp.get("/bank/mcp-abruf")
def mcp_bank_sync_page():
    company_id = request.args.get("company_id", type=int)
    if not company_id:
        return redirect(url_for("main.bank_page"))
    server_id = request.args.get("server_id", type=int)
    preview_wallet_id = (request.args.get("wallet_id") or "").strip() or None
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        servers = bank_sync_servers(session, tenant_id=company.tenant_id)
        server = next((item for item in servers if item.id == server_id), None) or (
            servers[0] if servers else None
        )
        bank_accounts = _bank_accounts(session, company.id)
        wallets = []
        error = None
        preview = None
        defaults: dict[str, dict] = {}
        if server is not None:
            try:
                with _caller(session_factory, server.id, company) as caller:
                    wallets = list_wallets(caller)
                    if preview_wallet_id:
                        preview = sync_mcp_bank_transactions(
                            session=session,
                            caller=caller,
                            company=company,
                            server=resolve_server(session, company, server.id),
                            bank_account_id=request.args.get("bank_account_id", type=int) or 0,
                            wallet_id=preview_wallet_id,
                            from_date=_date(request.args.get("from_date"), "Von"),
                            to_date=_date(request.args.get("to_date"), "Bis"),
                            dry_run=True,
                            changed_by=changed_by(),
                        )
            except McpBankSyncError as exc:
                error = str(exc)
            fallback = standard_account_id(bank_accounts, BANK)
            for wallet in wallets:
                # Die IBAN der Wallet schlägt das zuletzt gewählte Konto: Ein Abruf auf das
                # Sammelkonto „Bank“ soll sich nicht als Vorschlag festsetzen.
                account_id = (
                    bank_account_for_iban(session, company_id=company.id, iban=wallet.iban)
                    or last_bank_account_id(
                        session, company_id=company.id, server_id=server.id, wallet_id=wallet.id
                    )
                    or fallback
                )
                from_date = to_date = None
                if account_id:
                    from_date, to_date = default_sync_range(
                        session, company_id=company.id, bank_account_id=account_id
                    )
                defaults[wallet.id] = {
                    "bank_account_id": account_id,
                    "from_date": from_date,
                    "to_date": to_date,
                }
        return render_template(
            "mcp_bank_sync.html",
            company=company,
            servers=servers,
            server=server,
            wallets=wallets,
            defaults=defaults,
            bank_accounts=bank_accounts,
            preview=preview,
            error=error,
        )


@main_bp.post("/bank/mcp-abruf/uebernehmen")
def mcp_bank_sync_action():
    company_id = request.form.get("company_id", type=int)
    server_id = request.form.get("server_id", type=int)
    bank_account_id = request.form.get("bank_account_id", type=int)
    wallet_id = (request.form.get("wallet_id") or "").strip() or None
    if not company_id or not server_id or not bank_account_id or not wallet_id:
        flash("Gesellschaft, MCP-Server, Wallet und Bankkonto sind Pflichtfelder.", "error")
        return redirect(url_for("main.bank_page", company_id=company_id))
    session_factory = get_session_factory()
    with session_factory() as session:
        company = require_company_access(session, company_id)
        try:
            server = resolve_server(session, company, server_id)
            with _caller(session_factory, server.id, company) as caller:
                result = sync_mcp_bank_transactions(
                    session=session,
                    caller=caller,
                    company=company,
                    server=server,
                    bank_account_id=bank_account_id,
                    wallet_id=wallet_id,
                    from_date=_date(request.form.get("from_date"), "Von"),
                    to_date=_date(request.form.get("to_date"), "Bis"),
                    changed_by=changed_by(),
                )
        except McpBankSyncError as exc:
            flash(str(exc), "error")
            return redirect(
                url_for("main.mcp_bank_sync_page", company_id=company_id, server_id=server_id)
            )
    report = result.report
    zero_amount = (
        f", {result.zero_amount} über 0,00 € übersprungen" if result.zero_amount else ""
    )
    reasons = "".join(f" Zeile {error.line_number}: {error.message}" for error in report.errors)
    flash(
        f"{result.server_name}: {result.fetched} Umsätze abgerufen "
        f"({result.from_date:%d.%m.%Y}–{result.to_date:%d.%m.%Y}), {report.imported_rows} neu, "
        f"{result.known + report.duplicate_rows} schon vorhanden{zero_amount}, "
        f"{report.error_rows} Fehler.{reasons}",
        "success" if report.error_rows == 0 else "error",
    )
    if result.elsewhere:
        flash(
            f"{len(result.elsewhere)} der neuen Umsätze stehen mit gleichem Datum, Betrag und Text "
            f"schon auf Konto {', '.join(result.elsewhere_accounts)}. Falsches Bankkonto gewählt? "
            "Dann die neuen Umsätze verwerfen (Bankseite → Buchen … → Verwerfen).",
            "warning",
        )
    return redirect(
        url_for("main.bank_page", company_id=company_id, bank_account_id=bank_account_id)
    )
