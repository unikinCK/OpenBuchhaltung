"""Kontenrahmen-Prüfung: Altkonten des fehlerhaften SKR04-Imports erkennen.

Bis zur Korrektur im Oktober 2026 legte ``data/kontenrahmen/skr04.csv`` die
SKR04-Steuer- und Eigenkapitalkonten (1406/1401, 3806/3801, 2100/2180, 2970)
zusammen mit SKR03-Nummern für Kasse, Bank, Geldtransit, Forderungen,
Verbindlichkeiten, Aufwendungen und Erlöse an; „Gas, Strom, Wasser“ wurde wegen
eines unmaskierten Kommas zum Konto „Gas“ mit der Kontoart „Strom“.

Betroffene Gesellschaften werden bewusst nicht automatisch umgebaut:
Kontonummern sind unveränderlich (GoBD, Kontenhistorie). Die Prüfung nennt je
Altkonto die SKR04-Nummer, Buchungsanzahl und Saldo; die Buchhaltung überträgt
Salden per Umbuchung und deaktiviert die Altkonten. Deaktivierte Altkonten ohne
Saldo gelten als erledigt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.accounts import ACCOUNT_TYPES
from app.services.journal_entries import SOURCE_CARRYFORWARD
from domain.models import Account, JournalEntry, JournalEntryLine

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class LegacySkr04Account:
    """Konto der alten skr04.csv unter SKR03-Nummer und sein SKR04-Gegenstück."""

    code: str
    # Teil der Bezeichnung (kleingeschrieben): unterscheidet das Altkonto vom
    # gleichnummerigen echten SKR04-Konto und erkennt es auch nach Umbenennung.
    keyword: str
    skr04_code: str
    skr04_name: str


# SKR04-Nummern und -Bezeichnungen laut DATEV-Kontenrahmen SKR04 2026 (Art.-Nr. 11175).
LEGACY_SKR04_ACCOUNTS: tuple[LegacySkr04Account, ...] = (
    LegacySkr04Account(
        "0400",
        "immateriell",
        "0100",
        "Entgeltlich erworbene Konzessionen, gewerbliche Schutzrechte und ähnliche "
        "Rechte und Werte sowie Lizenzen an solchen Rechten und Werten",
    ),
    LegacySkr04Account("1000", "kasse", "1600", "Kasse"),
    LegacySkr04Account("1200", "bank", "1800", "Bank"),
    LegacySkr04Account("1360", "geldtransit", "1460", "Geldtransit"),
    LegacySkr04Account(
        "1400", "forderung", "1200", "Forderungen aus Lieferungen und Leistungen"
    ),
    LegacySkr04Account(
        "1600", "verbindlichkeit", "3300", "Verbindlichkeiten aus Lieferungen und Leistungen"
    ),
    LegacySkr04Account("3000", "rohstoff", "5100", "Einkauf Roh-, Hilfs- und Betriebsstoffe"),
    LegacySkr04Account("3200", "wareneingang", "5200", "Wareneingang"),
    LegacySkr04Account("3400", "fremdleistung", "5900", "Fremdleistungen"),
    LegacySkr04Account("4120", "löhne", "6000", "Löhne und Gehälter"),
    LegacySkr04Account("4130", "soziale", "6110", "Gesetzliche soziale Aufwendungen"),
    LegacySkr04Account("4210", "miete", "6310", "Miete (unbewegliche Wirtschaftsgüter)"),
    LegacySkr04Account("4240", "gas", "6325", "Gas, Strom, Wasser"),
    LegacySkr04Account("4360", "versicherung", "6400", "Versicherungen"),
    LegacySkr04Account("4600", "werbe", "6600", "Werbekosten"),
    LegacySkr04Account("4800", "instandhaltung", "6335", "Instandhaltung betrieblicher Räume"),
    LegacySkr04Account("4900", "aufwendungen", "6300", "Sonstige betriebliche Aufwendungen"),
    LegacySkr04Account("4970", "geldverkehr", "6855", "Nebenkosten des Geldverkehrs"),
    LegacySkr04Account(
        "6200",
        "sachanlagen",
        "6220",
        "Abschreibungen auf Sachanlagen (ohne AfA auf Fahrzeuge und Gebäude)",
    ),
    LegacySkr04Account("8000", "erlöse", "4400", "Erlöse 19 % USt"),
    LegacySkr04Account("8300", "erlöse", "4300", "Erlöse 7 % USt"),
)

# Konten, die nur der SKR04 kennt und die jeder SKR04-Import (alt wie neu)
# anlegt. Erst sie weisen eine Gesellschaft als SKR04 aus — sonst würden echte
# SKR03-Konten (1000 Kasse, 1200 Bank, …) als Altlast gemeldet.
SKR04_MARKERS: tuple[tuple[str, str], ...] = (
    ("1401", "vorsteuer"),
    ("1406", "vorsteuer"),
    ("3801", "umsatzsteuer"),
    ("3806", "umsatzsteuer"),
    ("2970", "gewinnvortrag"),
)


def _has_keyword(account: Account, keyword: str) -> bool:
    return keyword in (account.name or "").casefold()


def _posting_stats(
    session: Session, account_ids: Iterable[int]
) -> dict[int, tuple[int, Decimal]]:
    """Buchungszeilen und Saldo (Soll − Haben) je Konto, ohne Saldovorträge
    (die wiederholen nur die Vorjahressalden)."""
    ids = set(account_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(
            JournalEntryLine.account_id,
            func.count(JournalEntryLine.id),
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(
            JournalEntryLine.account_id.in_(ids),
            JournalEntry.source != SOURCE_CARRYFORWARD,
        )
        .group_by(JournalEntryLine.account_id)
    ).all()
    return {
        account_id: (count, (Decimal(debit) - Decimal(credit)).quantize(ZERO))
        for account_id, count, debit, credit in rows
    }


def _account_row(
    account: Account, stats: dict[int, tuple[int, Decimal]]
) -> dict[str, object] | None:
    """Prüfzeile eines Kontos; None, wenn es deaktiviert und ausgeglichen (erledigt) ist."""
    posting_count, balance = stats.get(account.id, (0, ZERO))
    if not account.is_active and balance == ZERO:
        return None
    return {
        "account_id": account.id,
        "code": account.code,
        "name": account.name,
        "account_type": account.account_type,
        "is_active": account.is_active,
        "posting_count": posting_count,
        "balance": str(balance),
    }


def check_account_chart(*, session: Session, company_id: int) -> dict[str, object]:
    """Prüft den Kontenplan einer Gesellschaft; ändert nichts."""
    accounts = (
        session.execute(
            select(Account).where(Account.company_id == company_id).order_by(Account.code)
        )
        .scalars()
        .all()
    )
    by_code = {account.code: account for account in accounts}

    markers = [
        code
        for code, keyword in SKR04_MARKERS
        if code in by_code and _has_keyword(by_code[code], keyword)
    ]
    legacy_matches: list[tuple[Account, LegacySkr04Account]] = []
    if markers:
        legacy_matches = [
            (by_code[legacy.code], legacy)
            for legacy in LEGACY_SKR04_ACCOUNTS
            if legacy.code in by_code and _has_keyword(by_code[legacy.code], legacy.keyword)
        ]
    unknown_type_accounts = [
        account for account in accounts if account.account_type not in ACCOUNT_TYPES
    ]
    stats = _posting_stats(
        session,
        [account.id for account, _ in legacy_matches]
        + [account.id for account in unknown_type_accounts],
    )

    legacy_ids = {account.id for account, _ in legacy_matches}
    legacy_rows = []
    for account, legacy in legacy_matches:
        row = _account_row(account, stats)
        if row is None:
            continue
        target = by_code.get(legacy.skr04_code)
        row["skr04_code"] = legacy.skr04_code
        row["skr04_name"] = legacy.skr04_name
        # Unter der SKR04-Nummer: nichts (missing), das SKR04-Konto (present) oder
        # ein anderes Altkonto, das die Nummer dauerhaft blockiert (occupied).
        if target is None:
            row["skr04_status"] = "missing"
        else:
            row["skr04_status"] = "occupied" if target.id in legacy_ids else "present"
        row["skr04_account"] = (
            {"account_id": target.id, "code": target.code, "name": target.name}
            if target is not None
            else None
        )
        legacy_rows.append(row)
    unknown_rows = [
        row
        for row in (_account_row(account, stats) for account in unknown_type_accounts)
        if row is not None
    ]

    warnings = []
    if legacy_rows:
        warnings.append(
            f"Altkonten aus dem fehlerhaften SKR04-Import ({len(legacy_rows)}): Ihre "
            "Nummern stammen aus dem SKR03 und bezeichnen im SKR04 andere Konten "
            "(u. a. relevant für DATEV-Export und E-Bilanz). Salden per Umbuchung auf "
            "die SKR04-Konten übertragen und die Altkonten danach deaktivieren — "
            "Kontonummern bleiben unverändert."
        )
    if unknown_rows:
        warnings.append(
            f"Konten mit unbekannter Kontoart ({len(unknown_rows)}): Bilanz, GuV und "
            "Jahresabschluss werten sie nicht aus. Die Kontoart lässt sich in der "
            "Kontenliste (bzw. per update_account mit account_type) korrigieren."
        )
    return {
        "company_id": company_id,
        "ok": not legacy_rows and not unknown_rows,
        "skr04_markers": markers,
        "legacy_skr04_accounts": legacy_rows,
        "unknown_account_types": unknown_rows,
        "warnings": warnings,
    }
