"""Funktionskonten, die Automatiken und Masken über ihre Standardnummer finden.

Einige Abläufe brauchen ein bestimmtes Sachkonto: Eigenüberträge laufen über
Geldtransit, der Jahresabschluss bucht gegen den Gewinnvortrag, die
Eröffnungsbilanz gleicht über das Saldenvortragskonto aus, Buchungsmasken
schlagen Bank und Verbindlichkeiten vor. Die Gesellschaft speichert ihren
Kontenrahmen nicht, die Suche muss SKR03 und SKR04 gleichermaßen bedienen.

Eine Kontonummer allein ist dabei kein Beleg, denn sie steht im jeweils anderen
Kontenrahmen für ein anderes Konto (DATEV-Kontenrahmen 2026, Art.-Nr. 11174/11175):

    SKR04 0860  Beteiligungen an Personengesellschaften    (SKR03: Gewinnvortrag)
    SKR04 1200  Forderungen aus Lieferungen und Leistungen (SKR03: Bank)
    SKR04 1360  Darlehen                                   (SKR03: Geldtransit)
    SKR04 1600  Kasse                                      (SKR03: Verbindlichkeiten aLuL)
    SKR03 1460  Zweifelhafte Forderungen                   (SKR04: Geldtransit)

Ein Konto zählt deshalb über seine Bezeichnung oder nur dann über die Nummer,
wenn die Kontoart die Verwechslung ausschließt. Unter mehreren Treffern gewinnt
die Standardnummer, SKR04 vor SKR03: Die SKR04-Nummern tragen im SKR03 nie diese
Bedeutung, beide Varianten stehen nur nach dem fehlerhaften SKR04-Import (siehe
``account_chart_check``) nebeneinander — dann ist die SKR04-Nummer die richtige.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from domain.models import Account


@dataclass(frozen=True, slots=True)
class StandardAccount:
    """Ein Funktionskonto und woran es in SKR03 und SKR04 zu erkennen ist."""

    label: str
    # Standardnummern in Vorzugsreihenfolge (SKR04 vor SKR03).
    codes: tuple[str, ...]
    # Teil der Bezeichnung (kleingeschrieben), an dem das Konto immer erkannt wird.
    name_keyword: str
    # Kontoarten, bei denen schon die Standardnummer genügt. Leer: nie, weil die
    # Nummer im anderen Kontenrahmen ein Konto derselben Kontoart bezeichnet;
    # None: immer, weil die Nummer in beiden Kontenrahmen gleich belegt ist.
    code_account_types: frozenset[str] | None = frozenset()

    def matches(self, account: Account) -> bool:
        if self.name_keyword in (account.name or "").casefold():
            return True
        if account.code not in self.codes:
            return False
        return self.code_account_types is None or account.account_type in self.code_account_types

    def rank(self, account: Account) -> tuple[int, str]:
        if account.code in self.codes:
            return self.codes.index(account.code), account.code
        return len(self.codes), account.code


# SKR04 1360 (Darlehen) und SKR03 1460 (Zweifelhafte Forderungen) sind ebenfalls
# Aktivkonten — nur die Bezeichnung entscheidet.
GELDTRANSIT = StandardAccount(
    label="Geldtransit",
    codes=("1460", "1360"),
    name_keyword="geldtransit",
)
# SKR04 0860 (Beteiligungen an Personengesellschaften) ist ein Aktivkonto.
GEWINNVORTRAG = StandardAccount(
    label="Gewinnvortrag vor Verwendung",
    codes=("2970", "0860"),
    name_keyword="gewinnvortrag",
    code_account_types=frozenset({"equity"}),
)
# Saldenvorträge Sachkonten/Debitoren/Kreditoren: in SKR03 und SKR04 gleich.
SALDENVORTRAG = StandardAccount(
    label="Saldenvorträge",
    codes=("9000", "9008", "9009"),
    name_keyword="saldenvortr",
    code_account_types=None,
)
# SKR04 1200 (Forderungen aus Lieferungen und Leistungen) ist ebenfalls ein Aktivkonto.
BANK = StandardAccount(
    label="Bank",
    codes=("1800", "1200"),
    name_keyword="bank",
)
# SKR04 1600 (Kasse) ist ein Aktivkonto, SKR03 3300 (Wareneingang) ein Aufwandskonto.
VERBINDLICHKEITEN_LUL = StandardAccount(
    label="Verbindlichkeiten aus Lieferungen und Leistungen",
    codes=("3300", "1600"),
    name_keyword="verbindlichkeiten aus lieferungen",
    code_account_types=frozenset({"liability"}),
)


def pick_standard_account(
    accounts: Iterable[Account], standard: StandardAccount
) -> Account | None:
    """Das passende aktive Konto aus einer bereits geladenen Kontenliste."""
    candidates = [
        account for account in accounts if account.is_active and standard.matches(account)
    ]
    return min(candidates, key=standard.rank, default=None)


def standard_account_id(accounts: Iterable[Account], standard: StandardAccount) -> int | None:
    """ID des passenden Kontos, z. B. für die Vorauswahl in Buchungsmasken."""
    account = pick_standard_account(accounts, standard)
    return account.id if account is not None else None


def find_standard_account(
    *, session: Session, company_id: int, standard: StandardAccount
) -> Account | None:
    """Sucht das Funktionskonto einer Gesellschaft (aktive Konten)."""
    candidates = session.execute(
        select(Account).where(
            Account.company_id == company_id,
            Account.is_active.is_(True),
            or_(
                Account.name.ilike(f"%{standard.name_keyword}%"),
                Account.code.in_(standard.codes),
            ),
        )
    ).scalars()
    return pick_standard_account(candidates, standard)
