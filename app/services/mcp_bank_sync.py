"""Bankumsätze über den MCP-Server der Bank abrufen – ohne KI (derzeit: Finom).

Finom bietet kein FinTS, und die Public API kennt keine Umsätze. Der MCP-Server von
Finom (``transactions__listWallets``/``transactions__listTransactions``) liefert sie
aber mit dem Login des Benutzers. Obi könnte die Werkzeuge im Chat aufrufen, müsste die
Umsätze dann aber selbst abschreiben: Werkzeug-Ergebnisse werden für das Modell gekürzt,
und Beträge oder IBANs liefen durch das Sprachmodell. Deshalb ruft OpenBuchhaltung die
Werkzeuge hier selbst auf, bildet die Umsätze auf Kontoauszugszeilen ab
(:func:`app.services.bank_statement.row_from_finom_mcp_transaction`) und importiert sie
über dieselbe Pipeline wie Datei-, FinTS- und Open-Banking-Abruf.

Besonderheiten von Finom:

* höchstens 10 Umsätze je Aufruf und kein Cursor, nur ``limitReached``. Geblättert wird
  über das Zeitfenster: ``dateBefore`` rückt auf den ältesten Zeitpunkt der Seite
  (plus eine Mikrosekunde, damit gleichzeitige Umsätze nicht verloren gehen); doppelt
  gelieferte fallen über die ID heraus.
* nur ``Completed``: vorgemerkte, geplante oder abgelehnte Zahlungen ändern sich noch.
* Zeitpunkte in UTC; Abrufzeitraum und Buchungstag gelten in Europe/Berlin.

Dedup: Umsätze, deren Transaktions-ID am Bankkonto schon als Bankreferenz steht, werden
vorab aussortiert – die Pipeline vergleicht Referenz *und* Text, und der Text kann sich
zwischen CSV-Export und MCP unterscheiden. Ohne Von-Datum beginnt der Abruf 7 Tage vor
dem Ende des letzten MCP-Abrufs, nie aber vor dem Tag nach dem letzten Umsatz ohne
UUID-Referenz (Altbestand aus CSV oder Handerfassung lässt sich nicht sicher
entdoppeln); beim ersten Abruf ab dem Tag nach dem letzten Umsatz, ohne Bestand 90 Tage.

Welche Wallet zu welchem Bankkonto gehört, merkt sich der Abruf im Audit-Log
(``bank_mcp_sync``) und schlägt sie beim nächsten Mal vor.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.bank_import import BankImportError, BankImportReport, import_bank_items
from app.services.bank_statement import (
    BOOKING_TIMEZONE,
    BankStatementRow,
    StatementItem,
    parse_utc_timestamp,
    row_from_finom_mcp_transaction,
)
from app.services.mcp_client import McpToolResult
from app.services.mcp_connections import McpConnectionError, cached_tools, server_toolbox
from domain.models import Account, AuditLog, BankTransaction, Company, McpServer

logger = logging.getLogger(__name__)

ADAPTER_FINOM = "finom"
FINOM_LIST_WALLETS = "transactions__listWallets"
FINOM_LIST_TRANSACTIONS = "transactions__listTransactions"
_ADAPTER_TOOLS = {ADAPTER_FINOM: (FINOM_LIST_WALLETS, FINOM_LIST_TRANSACTIONS)}

# Finom kappt ohnehin auf 10; mehr anzufragen schadet nicht, falls die Grenze steigt.
PAGE_LIMIT = 100
MAX_PAGES = 300
DEFAULT_FIRST_SYNC_DAYS = 90
RESYNC_OVERLAP_DAYS = 7
# So viele der jüngsten Umsätze prüft die Standard-Zeitraumwahl auf Finom-Referenzen.
_RANGE_SCAN_ROWS = 500
PREVIEW_ROWS = 200
AUDIT_ENTITY = "bank_mcp_sync"

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

ToolCaller = Callable[[str, dict[str, Any]], dict[str, Any]]


class McpBankSyncError(ValueError):
    """Der Abruf ist nicht möglich (Server, Zugang, Wallet, Zeitraum)."""


@dataclass(slots=True)
class BankWallet:
    id: str
    name: str
    iban: str | None
    currency: str | None
    is_default: bool = False
    is_closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "iban": self.iban,
            "currency": self.currency,
            "is_default": self.is_default,
            "is_closed": self.is_closed,
        }

    @property
    def label(self) -> str:
        parts = [self.name or "Wallet"]
        if self.iban:
            parts.append(self.iban)
        if self.currency:
            parts.append(self.currency)
        return " · ".join(parts)


@dataclass(slots=True)
class McpBankSyncResult:
    server_id: int
    server_name: str
    wallet: BankWallet
    bank_account_id: int
    from_date: date
    to_date: date
    dry_run: bool
    fetched: int = 0
    known: int = 0
    new_rows: list[BankStatementRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report: BankImportReport | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": "preview" if self.dry_run else "imported",
            "server_id": self.server_id,
            "server": self.server_name,
            "wallet": self.wallet.to_dict(),
            "bank_account_id": self.bank_account_id,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "fetched": self.fetched,
            "already_imported": self.known,
            "errors": self.errors,
        }
        if self.dry_run:
            body["new_rows"] = len(self.new_rows)
            body["rows"] = [
                {
                    "booking_date": row.booking_date.isoformat(),
                    "amount": str(row.amount),
                    "currency_code": row.currency_code,
                    "purpose": row.purpose,
                    "counterparty": row.counterparty,
                    "counterparty_iban": row.counterparty_iban,
                    "bank_reference": row.bank_reference,
                }
                for row in self.new_rows[:PREVIEW_ROWS]
            ]
            body["rows_truncated"] = len(self.new_rows) > PREVIEW_ROWS
        if self.report is not None:
            body["report"] = {
                "total_rows": self.report.total_rows,
                "imported_rows": self.report.imported_rows,
                "duplicate_rows": self.report.duplicate_rows,
                "error_rows": self.report.error_rows,
            }
        return body


# ---------------------------------------------------------------------------
# Server und Werkzeuge
# ---------------------------------------------------------------------------


def bank_adapter(server: McpServer) -> str | None:
    """Kennung des Bankabrufs, den der Server laut Werkzeugliste unterstützt."""
    names = {tool.name for tool in cached_tools(server)}
    allowed = set(server.allowed_tools) if server.allowed_tools is not None else None
    for adapter, required in _ADAPTER_TOOLS.items():
        if all(name in names and (allowed is None or name in allowed) for name in required):
            return adapter
    return None


def bank_sync_servers(session: Session, *, tenant_id: int | None) -> list[McpServer]:
    """Aktive MCP-Server des Mandanten, über die sich Bankumsätze abrufen lassen."""
    servers = (
        session.execute(
            select(McpServer)
            .where(McpServer.is_active.is_(True))
            .order_by(McpServer.name, McpServer.id)
        )
        .scalars()
        .all()
    )
    return [
        server
        for server in servers
        if (server.tenant_id is None or server.tenant_id == tenant_id) and bank_adapter(server)
    ]


def _result_json(result: McpToolResult, tool: str) -> dict[str, Any]:
    if result.is_error:
        raise McpBankSyncError(f"{tool} meldet einen Fehler: {result.as_text()[:300]}")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    try:
        parsed = json.loads(result.as_text())
    except ValueError as exc:
        raise McpBankSyncError(f"{tool} lieferte kein JSON.") from exc
    if not isinstance(parsed, dict):
        raise McpBankSyncError(f"{tool} lieferte kein JSON-Objekt.")
    return parsed


class ServerCaller:
    """Ruft Werkzeuge eines MCP-Servers mit dem Zugang des Benutzers (Kontextmanager)."""

    def __init__(
        self,
        *,
        session_factory,
        server_id: int,
        tenant_id: int | None,
        company_id: int | None,
        user: dict | None,
    ) -> None:
        try:
            self._toolbox = server_toolbox(
                session_factory=session_factory,
                server_id=server_id,
                tenant_id=tenant_id,
                company_id=company_id,
                user=user,
            )
        except McpConnectionError as exc:
            raise McpBankSyncError(str(exc)) from exc
        self._server_id = server_id

    def __call__(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._toolbox.call_remote(self._server_id, tool, arguments)
        except McpConnectionError as exc:
            raise McpBankSyncError(str(exc)) from exc
        return _result_json(result, tool)

    def close(self) -> None:
        self._toolbox.close()

    def __enter__(self) -> ServerCaller:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Finom
# ---------------------------------------------------------------------------


def list_wallets(caller: ToolCaller) -> list[BankWallet]:
    data = caller(FINOM_LIST_WALLETS, {})
    wallets = []
    for raw in data.get("wallets") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            continue
        wallets.append(
            BankWallet(
                id=raw["id"],
                name=str(raw.get("name") or "")[:120],
                iban=(str(raw["iban"]).replace(" ", "").upper() if raw.get("iban") else None),
                currency=(str(raw["currency"]).upper() if raw.get("currency") else None),
                is_default=raw.get("isDefault") is True,
                is_closed=raw.get("isClosed") is True,
            )
        )
    return wallets


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _day_start_utc(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=BOOKING_TIMEZONE).astimezone(timezone.utc)


def fetch_transactions(
    caller: ToolCaller, *, wallet_id: str, from_date: date, to_date: date
) -> list[dict[str, Any]]:
    """Alle abgeschlossenen Umsätze der Wallet im Zeitraum (Tage in Europe/Berlin)."""
    lower = _day_start_utc(from_date)
    before = _day_start_utc(to_date + timedelta(days=1))
    seen: dict[str, dict[str, Any]] = {}
    for _page in range(MAX_PAGES):
        data = caller(
            FINOM_LIST_TRANSACTIONS,
            {
                "accountIds": [wallet_id],
                "statuses": ["Completed"],
                "dateFrom": _utc_iso(lower),
                "dateBefore": _utc_iso(before),
                "limit": PAGE_LIMIT,
            },
        )
        items = [item for item in data.get("transactions") or [] if isinstance(item, dict)]
        new = [item for item in items if item.get("id") and str(item["id"]) not in seen]
        for item in new:
            seen[str(item["id"])] = item
        if not data.get("limitReached"):
            return list(seen.values())
        moments = [
            moment
            for item in items
            if (moment := parse_utc_timestamp(item.get("executed") or item.get("created")))
        ]
        if not new or not moments:
            # Mehr Umsätze mit demselben Zeitpunkt, als eine Seite fasst – oder der Server
            # meldet „mehr“, ohne weiterzukommen. Lieber abbrechen als still etwas auslassen.
            raise McpBankSyncError(
                "Der MCP-Server liefert keine weiteren Umsätze, obwohl er mehr meldet; "
                "der Abruf wurde abgebrochen, ohne etwas zu importieren."
            )
        before = min(moments) + timedelta(microseconds=1)
    raise McpBankSyncError(
        f"Mehr als {MAX_PAGES} Seiten Umsätze – bitte einen kürzeren Zeitraum abrufen."
    )


# ---------------------------------------------------------------------------
# Zeitraum und Wallet-Vorschlag
# ---------------------------------------------------------------------------


def _last_sync_to(session: Session, *, company_id: int, bank_account_id: int) -> date | None:
    payload = session.execute(
        select(AuditLog.payload)
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_type == AUDIT_ENTITY,
            AuditLog.entity_id == str(bank_account_id),
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    try:
        return date.fromisoformat(payload["to_date"]) if isinstance(payload, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


def default_sync_range(
    session: Session, *, company_id: int, bank_account_id: int, today: date | None = None
) -> tuple[date, date]:
    """Standard-Zeitraum: nach einem früheren MCP-Abruf mit 7 Tagen Überlappung, aber nie
    vor dem Tag nach dem letzten Umsatz ohne UUID-Referenz; beim ersten Abruf ab dem Tag
    nach dem letzten vorhandenen Umsatz (andere Quellen tragen andere Referenzen), sonst
    90 Tage zurück."""
    to_date = today or datetime.now(BOOKING_TIMEZONE).date()
    rows = session.execute(
        select(BankTransaction.booking_date, BankTransaction.bank_reference)
        .where(
            BankTransaction.company_id == company_id,
            BankTransaction.bank_account_id == bank_account_id,
        )
        .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
        .limit(_RANGE_SCAN_ROWS)
    ).all()
    newest_any = rows[0][0] if rows else None
    newest_other = next(
        (day for day, reference in rows if not (reference and _UUID.match(reference))), None
    )
    last_to = _last_sync_to(session, company_id=company_id, bank_account_id=bank_account_id)
    if last_to is not None:
        from_date = last_to - timedelta(days=RESYNC_OVERLAP_DAYS)
        if newest_other is not None:
            from_date = max(from_date, newest_other + timedelta(days=1))
    elif newest_any is not None:
        from_date = newest_any + timedelta(days=1)
    else:
        from_date = to_date - timedelta(days=DEFAULT_FIRST_SYNC_DAYS)
    return min(from_date, to_date), to_date


def last_wallet_id(
    session: Session, *, company_id: int, bank_account_id: int, server_id: int
) -> str | None:
    """Die Wallet, aus der zuletzt in dieses Bankkonto importiert wurde."""
    entries = session.execute(
        select(AuditLog.payload)
        .where(
            AuditLog.company_id == company_id,
            AuditLog.entity_type == AUDIT_ENTITY,
            AuditLog.entity_id == str(bank_account_id),
        )
        .order_by(AuditLog.id.desc())
        .limit(20)
    ).scalars()
    for payload in entries:
        if isinstance(payload, dict) and payload.get("server_id") == server_id:
            wallet_id = payload.get("wallet_id")
            return wallet_id if isinstance(wallet_id, str) else None
    return None


def last_bank_account_id(
    session: Session, *, company_id: int, server_id: int, wallet_id: str
) -> int | None:
    """Das Bankkonto, in das zuletzt aus dieser Wallet importiert wurde."""
    entries = session.execute(
        select(AuditLog.entity_id, AuditLog.payload)
        .where(AuditLog.company_id == company_id, AuditLog.entity_type == AUDIT_ENTITY)
        .order_by(AuditLog.id.desc())
        .limit(200)
    ).all()
    for entity_id, payload in entries:
        if (
            isinstance(payload, dict)
            and payload.get("server_id") == server_id
            and payload.get("wallet_id") == wallet_id
            and str(entity_id).isdigit()
        ):
            return int(entity_id)
    return None


def default_wallet(
    wallets: list[BankWallet],
    *,
    session: Session,
    company_id: int,
    bank_account_id: int,
    server_id: int,
) -> BankWallet | None:
    remembered = last_wallet_id(
        session, company_id=company_id, bank_account_id=bank_account_id, server_id=server_id
    )
    open_wallets = [wallet for wallet in wallets if not wallet.is_closed]
    return (
        next((wallet for wallet in wallets if wallet.id == remembered), None)
        or next((wallet for wallet in open_wallets if wallet.is_default), None)
        or (open_wallets[0] if len(open_wallets) == 1 else None)
    )


# ---------------------------------------------------------------------------
# Abruf
# ---------------------------------------------------------------------------


def resolve_bank_account(session: Session, company: Company, bank_account_id: int) -> Account:
    account = session.get(Account, bank_account_id)
    if (
        account is None
        or account.company_id != company.id
        or account.account_type != "asset"
        or account.subledger is not None
    ):
        raise McpBankSyncError("Bankkonto nicht gefunden.")
    return account


def resolve_server(session: Session, company: Company, server_id: int) -> McpServer:
    server = session.get(McpServer, server_id)
    if (
        server is None
        or not server.is_active
        or (server.tenant_id is not None and server.tenant_id != company.tenant_id)
    ):
        raise McpBankSyncError("MCP-Server nicht gefunden.")
    if bank_adapter(server) is None:
        raise McpBankSyncError(
            f"{server.name} bietet keinen Umsatzabruf (benötigt {FINOM_LIST_WALLETS} und "
            f"{FINOM_LIST_TRANSACTIONS}, freigegeben für Obi)."
        )
    return server


def _known_references(
    session: Session, *, company_id: int, bank_account_id: int, references: list[str]
) -> set[str]:
    known: set[str] = set()
    for start in range(0, len(references), 500):
        chunk = references[start : start + 500]
        known.update(
            session.execute(
                select(BankTransaction.bank_reference).where(
                    BankTransaction.company_id == company_id,
                    BankTransaction.bank_account_id == bank_account_id,
                    BankTransaction.bank_reference.in_(chunk),
                )
            ).scalars()
        )
    return known


def sync_mcp_bank_transactions(
    *,
    session: Session,
    caller: ToolCaller,
    company: Company,
    server: McpServer,
    bank_account_id: int,
    wallet_id: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    dry_run: bool = False,
    changed_by: str,
) -> McpBankSyncResult:
    """Ruft die Umsätze einer Wallet ab und importiert sie (oder zeigt sie nur an)."""
    account = resolve_bank_account(session, company, bank_account_id)
    wallets = list_wallets(caller)
    if wallet_id:
        wallet = next((item for item in wallets if item.id == wallet_id), None)
        if wallet is None:
            raise McpBankSyncError("Die Wallet gibt es bei diesem Zugang nicht.")
    else:
        wallet = default_wallet(
            wallets,
            session=session,
            company_id=company.id,
            bank_account_id=account.id,
            server_id=server.id,
        )
        if wallet is None:
            raise McpBankSyncError(
                "Bitte die Wallet wählen: "
                + "; ".join(f"{item.label} (id {item.id})" for item in wallets)
            )

    default_from, default_to = default_sync_range(
        session, company_id=company.id, bank_account_id=account.id
    )
    to_date = to_date or default_to
    from_date = from_date or min(default_from, to_date)
    if from_date > to_date:
        raise McpBankSyncError("Das Von-Datum darf nicht nach dem Bis-Datum liegen.")

    transactions = fetch_transactions(
        caller, wallet_id=wallet.id, from_date=from_date, to_date=to_date
    )
    # Älteste zuerst, wie in einem Kontoauszug.
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    transactions.sort(
        key=lambda item: parse_utc_timestamp(item.get("executed") or item.get("created")) or epoch
    )
    items: list[StatementItem] = [
        row_from_finom_mcp_transaction(position, item)
        for position, item in enumerate(transactions, start=1)
    ]
    known = _known_references(
        session,
        company_id=company.id,
        bank_account_id=account.id,
        references=[
            item.bank_reference
            for item in items
            if isinstance(item, BankStatementRow) and item.bank_reference
        ],
    )
    fresh = [
        item
        for item in items
        if not (isinstance(item, BankStatementRow) and item.bank_reference in known)
    ]
    result = McpBankSyncResult(
        server_id=server.id,
        server_name=server.name,
        wallet=wallet,
        bank_account_id=account.id,
        from_date=from_date,
        to_date=to_date,
        dry_run=dry_run,
        fetched=len(items),
        known=len(items) - len(fresh),
        new_rows=[item for item in fresh if isinstance(item, BankStatementRow)],
        errors=[
            f"Umsatz {item.position}: {item.message}"
            for item in fresh
            if not isinstance(item, BankStatementRow)
        ],
    )
    if dry_run:
        return result

    try:
        result.report = import_bank_items(
            session=session,
            company_id=company.id,
            bank_account_id=account.id,
            items=fresh,
            changed_by=changed_by,
            source=f"mcp_{bank_adapter(server)}",
        )
    except BankImportError as exc:
        raise McpBankSyncError(str(exc)) from exc
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type=AUDIT_ENTITY,
        entity_id=str(account.id),
        action="synced",
        changed_by=changed_by,
        payload={
            "server_id": server.id,
            "server": server.name,
            "wallet_id": wallet.id,
            "wallet": wallet.name,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "fetched": result.fetched,
            "already_imported": result.known,
            "imported": result.report.imported_rows,
            "duplicates": result.report.duplicate_rows,
            "errors": result.report.error_rows,
        },
    )
    session.commit()
    return result
