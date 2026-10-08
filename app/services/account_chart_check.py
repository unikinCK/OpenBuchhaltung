"""Kontenrahmen-Prüfung: Altbestände des fehlerhaften SKR04-Imports erkennen.

Bis zur Korrektur im Oktober 2026 legte ``data/kontenrahmen/skr04.csv`` echte
SKR04-Konten (Vorsteuer 1406/1401, Umsatzsteuer 3806/3801, Privat 2100/2180,
Gewinnvortrag 2970, Technische Anlagen 0420) zusammen mit SKR03-Nummern für
Kasse, Bank, Geldtransit, Forderungen, Verbindlichkeiten, Aufwendungen und
Erlöse an; „Gas, Strom, Wasser“ wurde wegen eines unmaskierten Kommas zum Konto
„Gas“ mit der Kontoart „Strom“.

Welche Hälfte davon falsch ist, entscheidet die weitere Buchhaltung der
Gesellschaft. Die Prüfung bestimmt deshalb den vorherrschenden Kontenrahmen an
den Konten außerhalb des alten Imports (``detect_dominant_chart``):

* SKR04: Die SKR03-Nummern sind Altkonten mit SKR04-Gegenstück
  (``LEGACY_SKR04_ACCOUNTS``).
* SKR03: Die SKR04-Nummern sind Fremdkonten mit SKR03-Gegenstück
  (``FOREIGN_SKR04_ACCOUNTS``); dazu kommen Hinweise auf SKR03-Nummern der alten
  Datei, die laut DATEV ein anderes Konto bezeichnen
  (``NONSTANDARD_SKR03_ACCOUNTS``).

Betroffene Gesellschaften werden bewusst nicht automatisch umgebaut:
Kontonummern sind unveränderlich (GoBD, Kontenhistorie). Die Prüfung nennt je
Konto die richtige Nummer, Buchungsanzahl und Saldo; die Buchhaltung überträgt
Salden per Umbuchung und deaktiviert die Konten. Deaktivierte Konten ohne Saldo
gelten als erledigt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.services.accounts import ACCOUNT_TYPES
from app.services.journal_entries import SOURCE_CARRYFORWARD
from domain.models import Account, JournalEntry, JournalEntryLine, TaxCode

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


@dataclass(frozen=True, slots=True)
class ForeignSkr04Account:
    """SKR04-Konto der alten skr04.csv und sein Gegenstück im SKR03."""

    code: str
    # Teil der Bezeichnung (kleingeschrieben), an dem das Fremdkonto und das
    # SKR03-Gegenkonto erkannt werden — eine anders belegte SKR03-Nummer (etwa
    # 1800 „Bank“ aus einem SKR04-Import) zählt so nicht als Gegenkonto.
    keyword: str
    skr03_code: str
    skr03_name: str
    # Was die Nummer im SKR03 bezeichnet; None: Dort ist sie nicht vergeben.
    skr03_meaning: str | None = None


# SKR03-Nummern und -Bezeichnungen laut DATEV-Kontenrahmen SKR03 2026 (Art.-Nr. 11174).
FOREIGN_SKR04_ACCOUNTS: tuple[ForeignSkr04Account, ...] = (
    ForeignSkr04Account(
        "0420", "technische", "0200", "Technische Anlagen und Maschinen", "Büroeinrichtung"
    ),
    ForeignSkr04Account("1401", "vorsteuer", "1571", "Abziehbare Vorsteuer 7 %", "reserviert"),
    ForeignSkr04Account("1406", "vorsteuer", "1576", "Abziehbare Vorsteuer 19 %", "reserviert"),
    ForeignSkr04Account(
        "2100", "privat", "1800", "Privatentnahmen allgemein", "Zinsen und ähnliche Aufwendungen"
    ),
    ForeignSkr04Account("2180", "privat", "1890", "Privateinlagen"),
    # Der SKR03 führt den Gewinnvortrag auf 0860 (2860 ist der Vortrag nach Verwendung).
    ForeignSkr04Account("2970", "vortrag", "0860", "Gewinnvortrag vor Verwendung"),
    ForeignSkr04Account("3801", "umsatzsteuer", "1771", "Umsatzsteuer 7 %"),
    ForeignSkr04Account("3806", "umsatzsteuer", "1776", "Umsatzsteuer 19 %"),
)


@dataclass(frozen=True, slots=True)
class NonstandardSkr03Account:
    """SKR03-Nummer der alten skr04.csv, die laut DATEV etwas anderes bezeichnet."""

    code: str
    # Teil der Bezeichnung, an dem das Konto der alten Datei erkannt wird.
    keyword: str
    # Was die Nummer im SKR03 bezeichnet.
    skr03_meaning: str
    # Standardkonto im SKR03 für das, was die Bezeichnung verspricht.
    skr03_code: str
    skr03_name: str
    # Teil der Bezeichnung, an dem das Standardkonto erkannt wird.
    skr03_keyword: str


NONSTANDARD_SKR03_ACCOUNTS: tuple[NonstandardSkr03Account, ...] = (
    NonstandardSkr03Account(
        "0400",
        "immateriell",
        "Betriebsausstattung",
        "0010",
        "Entgeltlich erworbene Konzessionen, gewerbliche Schutzrechte und ähnliche "
        "Rechte und Werte sowie Lizenzen an solchen Rechten und Werten",
        "",
    ),
    NonstandardSkr03Account(
        "3400",
        "fremdleistung",
        "Wareneingang 19 % Vorsteuer (Automatikkonto)",
        "3100",
        "Fremdleistungen",
        "fremdleistung",
    ),
    NonstandardSkr03Account(
        "4800",
        "räume",
        "Reparaturen und Instandhaltungen von technischen Anlagen und Maschinen",
        "4260",
        "Instandhaltung betrieblicher Räume",
        "räume",
    ),
    NonstandardSkr03Account(
        "6200",
        "abschreibung",
        "kein Einzelkonto (Bereich 6000–6999 „Sonstige betriebliche Aufwendungen“)",
        "4830",
        "Abschreibungen auf Sachanlagen (ohne AfA auf Fahrzeuge und Gebäude)",
        "abschreibung",
    ),
    NonstandardSkr03Account(
        "8000",
        "erlös",
        "kein Einzelkonto (Bereich 8000–8099 „Umsatzerlöse (zur freien Verfügung)“)",
        "8400",
        "Erlöse 19 % USt",
        "erlös",
    ),
)

# Konten, die nur der SKR04 kennt und die jeder SKR04-Import (alt wie neu)
# anlegt. Erst sie weisen auf einen SKR04-Import hin — sonst würden echte
# SKR03-Konten (1000 Kasse, 1200 Bank, …) als Altlast gemeldet.
SKR04_MARKERS: tuple[tuple[str, str], ...] = (
    ("1401", "vorsteuer"),
    ("1406", "vorsteuer"),
    ("3801", "umsatzsteuer"),
    ("3806", "umsatzsteuer"),
    ("2970", "gewinnvortrag"),
)

# Kontenbereiche, in denen eine Kontoart nur in einem der beiden Kontenrahmen
# vorkommt (DATEV-Kontenrahmen 2026). Der SKR03 gliedert nach dem Prozess
# (Klasse 0 Anlagen und Kapital, 1 Finanz- und Privatkonten, 2 neutrale
# Aufwendungen und Erträge, 3 Wareneingang und Bestände, 4 Aufwendungen,
# 7 Bestände an Erzeugnissen, 8 Erlöse), der SKR04 nach dem Abschluss (0
# Anlagevermögen, 1 Umlaufvermögen, 2 Eigenkapital, 3 Fremdkapital, 4 Erträge,
# 5 und 6 Aufwendungen, 7 weitere Erträge und Aufwendungen). Ohne Aussage
# bleiben Anlage- und Umlaufvermögen der Klassen 0 und 1, die Klasse 9 und
# Eigenkapital in 2400–2899 (SKR03-Ergebnisverwendung, SKR04-Teilhafterkonten).
CHART_TYPICAL_RANGES: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {
    "skr03": {
        "asset": ((3000, 3999), (7000, 7999)),
        "liability": ((0, 999), (1600, 1799)),
        "equity": ((0, 999), (1800, 1999)),
        "income": ((2000, 2999), (8000, 8999)),
        "expense": ((2000, 4999), (8000, 8999)),
    },
    "skr04": {
        "liability": ((2000, 3999),),
        "equity": ((2000, 2399), (2900, 2999)),
        "income": ((4000, 4999), (7000, 7999)),
        # Die SKR03-Klassen 5 und 6 sind dort nur als Bereiche ohne Einzelkonten ausgewiesen.
        "expense": ((5000, 7999),),
    },
}


def _has_keyword(account: Account, keyword: str) -> bool:
    return keyword in (account.name or "").casefold()


def _normalized_type(account_type: str | None) -> str | None:
    return "income" if account_type == "revenue" else account_type


def _posting_stats(
    session: Session, company_id: int, account_ids: Iterable[int] | None = None
) -> dict[int, tuple[int, Decimal]]:
    """Buchungszeilen und Saldo (Soll − Haben) je Konto, ohne Saldovorträge
    (die wiederholen nur die Vorjahressalden). Ohne ``account_ids`` für alle Konten."""
    query = (
        select(
            JournalEntryLine.account_id,
            func.count(JournalEntryLine.id),
            func.coalesce(func.sum(JournalEntryLine.debit_amount), 0),
            func.coalesce(func.sum(JournalEntryLine.credit_amount), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalEntryLine.journal_entry_id)
        .where(
            JournalEntry.company_id == company_id,
            JournalEntry.source != SOURCE_CARRYFORWARD,
        )
        .group_by(JournalEntryLine.account_id)
    )
    if account_ids is not None:
        ids = set(account_ids)
        if not ids:
            return {}
        query = query.where(JournalEntryLine.account_id.in_(ids))
    return {
        account_id: (count, (Decimal(debit) - Decimal(credit)).quantize(ZERO))
        for account_id, count, debit, credit in session.execute(query).all()
    }


def _from_old_import(account: Account) -> bool:
    """Stammt das Konto (nach Nummer und Bezeichnung) aus der alten skr04.csv?"""
    return any(
        account.code == entry.code and _has_keyword(account, entry.keyword)
        for entry in (*LEGACY_SKR04_ACCOUNTS, *FOREIGN_SKR04_ACCOUNTS)
    )


def _typical_chart(account: Account) -> str | None:
    """SKR03 oder SKR04, wenn Nummernbereich und Kontoart nur zu einem Rahmen passen."""
    code = account.code or ""
    if len(code) != 4 or not (code.isascii() and code.isdigit()):
        return None
    number = int(code)
    account_type = _normalized_type(account.account_type)
    for chart, ranges in CHART_TYPICAL_RANGES.items():
        if any(low <= number <= high for low, high in ranges.get(account_type, ())):
            return chart
    return None


def detect_dominant_chart(
    accounts: Iterable[Account], stats: dict[int, tuple[int, Decimal]]
) -> tuple[str, dict[str, dict[str, int]]]:
    """Vorherrschender Kontenrahmen einer Gesellschaft mit SKR04-Import.

    Maßgeblich sind die Konten außerhalb der alten skr04.csv — deren Konten stehen
    in jedem Fall im Kontenplan und werden in jedem Fall bebucht. Je Kontenrahmen
    zählen die typischen Konten (aktiv oder bebucht) und ihre Buchungszeilen
    zusammen: So entscheidet weder ein unbebucht nachimportierter Kontenrahmen
    noch ein einzelnes viel bebuchtes Konto allein. Ohne Unterschied gilt der
    importierte SKR04.
    """
    evidence = {chart: {"accounts": 0, "posting_count": 0} for chart in CHART_TYPICAL_RANGES}
    for account in accounts:
        chart = None if _from_old_import(account) else _typical_chart(account)
        posting_count = stats.get(account.id, (0, ZERO))[0]
        if chart is None or (not account.is_active and posting_count == 0):
            continue
        evidence[chart]["accounts"] += 1
        evidence[chart]["posting_count"] += posting_count
    score = {chart: sum(counts.values()) for chart, counts in evidence.items()}
    return ("skr03" if score["skr03"] > score["skr04"] else "skr04"), evidence


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


def _account_ref(account: Account | None) -> dict[str, object] | None:
    if account is None:
        return None
    return {"account_id": account.id, "code": account.code, "name": account.name}


def _target_status(target: Account | None, source: Account, keyword: str) -> str:
    """Was unter der richtigen Nummer steht: nichts (missing), das passende Konto
    gleicher Kontoart (present) oder ein anderes Konto (occupied)."""
    if target is None:
        return "missing"
    same_type = _normalized_type(target.account_type) == _normalized_type(source.account_type)
    return "present" if same_type and _has_keyword(target, keyword) else "occupied"


def _legacy_rows(
    by_code: dict[str, Account], stats: dict[int, tuple[int, Decimal]]
) -> list[dict[str, object]]:
    matches = [
        (by_code[legacy.code], legacy)
        for legacy in LEGACY_SKR04_ACCOUNTS
        if legacy.code in by_code and _has_keyword(by_code[legacy.code], legacy.keyword)
    ]
    legacy_ids = {account.id for account, _ in matches}
    rows = []
    for account, legacy in matches:
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
        row["skr04_account"] = _account_ref(target)
        rows.append(row)
    return rows


def _tax_codes_by_account(session: Session, company_id: int) -> dict[int, list[str]]:
    """Steuercodes je Steuerkonto: Über sie erkennt die UStVA Steuerzeilen."""
    result: dict[int, list[str]] = {}
    for account_id, code in session.execute(
        select(TaxCode.vat_account_id, TaxCode.code)
        .where(TaxCode.company_id == company_id, TaxCode.vat_account_id.is_not(None))
        .order_by(TaxCode.code)
    ):
        result.setdefault(account_id, []).append(code)
    return result


def _foreign_rows(
    by_code: dict[str, Account],
    stats: dict[int, tuple[int, Decimal]],
    tax_codes: dict[int, list[str]],
) -> list[dict[str, object]]:
    rows = []
    for foreign in FOREIGN_SKR04_ACCOUNTS:
        account = by_code.get(foreign.code)
        if account is None or not _has_keyword(account, foreign.keyword):
            continue
        row = _account_row(account, stats)
        if row is None:
            continue
        target = by_code.get(foreign.skr03_code)
        row["skr03_code"] = foreign.skr03_code
        row["skr03_name"] = foreign.skr03_name
        row["skr03_meaning"] = foreign.skr03_meaning
        row["skr03_status"] = _target_status(target, account, foreign.keyword)
        row["skr03_account"] = _account_ref(target)
        row["tax_codes"] = tax_codes.get(account.id, [])
        rows.append(row)
    return rows


def _nonstandard_rows(
    by_code: dict[str, Account], stats: dict[int, tuple[int, Decimal]]
) -> list[dict[str, object]]:
    rows = []
    for entry in NONSTANDARD_SKR03_ACCOUNTS:
        account = by_code.get(entry.code)
        if account is None or not _has_keyword(account, entry.keyword):
            continue
        row = _account_row(account, stats)
        if row is None:
            continue
        target = by_code.get(entry.skr03_code)
        row["skr03_meaning"] = entry.skr03_meaning
        row["skr03_code"] = entry.skr03_code
        row["skr03_name"] = entry.skr03_name
        row["skr03_status"] = _target_status(target, account, entry.skr03_keyword)
        row["skr03_account"] = _account_ref(target)
        rows.append(row)
    return rows


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
    unknown_type_accounts = [
        account for account in accounts if account.account_type not in ACCOUNT_TYPES
    ]
    # Den Kontenrahmen bestimmen nur Gesellschaften mit SKR04-Import; sonst genügen
    # die Buchungen der Konten mit unbekannter Kontoart.
    if markers:
        stats = _posting_stats(session, company_id)
    else:
        stats = _posting_stats(
            session, company_id, [account.id for account in unknown_type_accounts]
        )

    dominant_chart = evidence = None
    legacy_rows: list[dict[str, object]] = []
    foreign_rows: list[dict[str, object]] = []
    nonstandard_rows: list[dict[str, object]] = []
    if markers:
        dominant_chart, evidence = detect_dominant_chart(accounts, stats)
        if dominant_chart == "skr04":
            legacy_rows = _legacy_rows(by_code, stats)
        else:
            foreign_rows = _foreign_rows(by_code, stats, _tax_codes_by_account(session, company_id))
            nonstandard_rows = _nonstandard_rows(by_code, stats)
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
    if foreign_rows:
        warnings.append(
            f"SKR04-Konten aus dem fehlerhaften SKR04-Import ({len(foreign_rows)}): Der "
            "Kontenplan folgt überwiegend dem SKR03, der diese Nummern nicht oder für "
            "andere Konten vergibt (u. a. relevant für DATEV-Export und E-Bilanz). Salden "
            "per Umbuchung auf die SKR03-Konten übertragen und die SKR04-Konten danach "
            "deaktivieren — Kontonummern bleiben unverändert."
        )
    taxed = [row for row in foreign_rows if row["tax_codes"]]
    if taxed:
        listed = "; ".join(f"{row['code']}: {', '.join(row['tax_codes'])}" for row in taxed)
        warnings.append(
            f"Steuerkonten mit Steuercode nicht umbuchen ({listed}): Die UStVA erkennt "
            "Umsatz- und Vorsteuerzeilen über die Steuerkonten der Steuercodes, eine "
            "Umbuchung änderte die Steuer des Umbuchungszeitraums. Steuercodes lassen "
            "sich derzeit nicht auf ein anderes Steuerkonto umstellen."
        )
    if nonstandard_rows:
        warnings.append(
            f"Hinweis ({len(nonstandard_rows)}): Diese Konten aus dem alten SKR04-Import "
            "tragen SKR03-Nummern, die laut DATEV ein anderes Konto bezeichnen oder kein "
            "Einzelkonto sind (u. a. relevant für DATEV-Export und E-Bilanz). Salden auf "
            "das SKR03-Standardkonto umbuchen und die Konten deaktivieren."
        )
    if unknown_rows:
        warnings.append(
            f"Konten mit unbekannter Kontoart ({len(unknown_rows)}): Bilanz, GuV und "
            "Jahresabschluss werten sie nicht aus. Die Kontoart lässt sich in der "
            "Kontenliste (bzw. per update_account mit account_type) korrigieren."
        )
    return {
        "company_id": company_id,
        # Hinweise auf Nicht-Standardnummern zählen nicht als Befund.
        "ok": not legacy_rows and not foreign_rows and not unknown_rows,
        "skr04_markers": markers,
        "dominant_chart": dominant_chart,
        "chart_evidence": evidence,
        "legacy_skr04_accounts": legacy_rows,
        "foreign_skr04_accounts": foreign_rows,
        "nonstandard_skr03_accounts": nonstandard_rows,
        "unknown_account_types": unknown_rows,
        "warnings": warnings,
    }
