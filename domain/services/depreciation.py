"""Abschreibungsverfahren (AfA) nach HGB und Steuerrecht.

Reine Berechnungslogik ohne Persistenz. Erzeugt aus den Stammdaten eines
Anlageguts einen Abschreibungsplan (eine Zeile je Wirtschaftsjahr) mit
Buchwertverlauf. Bewusst frei von SQLAlchemy/Flask, damit die Verfahren
unabhängig unit-testbar sind.

Unterstützte Verfahren:

* ``linear``       – Lineare AfA (§ 7 Abs. 1 EStG, § 253 Abs. 3 HGB). Im
                     Anschaffungsjahr zeitanteilig ab dem Monat der
                     Inbetriebnahme (pro rata temporis, § 7 Abs. 1 Satz 4 EStG).
* ``degressive``   – Geometrisch-degressive AfA (§ 7 Abs. 2 EStG) vom
                     Buchwert, mit automatischem Übergang zur linearen AfA,
                     sobald diese den höheren Betrag liefert.
* ``leistung``     – Leistungs-AfA (§ 7 Abs. 1 Satz 6 EStG). Abschreibung
                     nach tatsächlicher Nutzung; benötigt die Leistungsmengen
                     je Jahr.
* ``gwg``          – Sofortabschreibung geringwertiger Wirtschaftsgüter
                     (§ 6 Abs. 2 EStG): volle Abschreibung im Zugangsjahr.
* ``sammelposten`` – Poolabschreibung (§ 6 Abs. 2a EStG): gleichmäßig über
                     fünf Jahre (20 % p. a.), ohne Monatsanteil und ohne
                     Berücksichtigung von Abgängen.
* ``manuell``      – Kein automatischer Plan; Abschreibung wird ausschließlich
                     als außerplanmäßige/manuelle Buchung erfasst.

Restwert (``residual_value``) bzw. Erinnerungswert (``keep_memo_value`` = 1,00 €)
bilden die Untergrenze des Buchwerts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MEMO_VALUE = Decimal("1.00")

LINEAR = "linear"
DEGRESSIVE = "degressive"
LEISTUNG = "leistung"
GWG = "gwg"
SAMMELPOSTEN = "sammelposten"
MANUELL = "manuell"

METHODS: frozenset[str] = frozenset(
    {LINEAR, DEGRESSIVE, LEISTUNG, GWG, SAMMELPOSTEN, MANUELL}
)

# Verfahren, die einen automatisch berechenbaren Mehrjahresplan besitzen.
SCHEDULED_METHODS: frozenset[str] = frozenset(
    {LINEAR, DEGRESSIVE, LEISTUNG, GWG, SAMMELPOSTEN}
)

# Gesetzliche Grenzwerte (Netto) für die Sofort-/Poolabschreibung – als
# Orientierung; die eigentliche Verfahrenswahl trifft der Anwender.
GWG_THRESHOLD = Decimal("800.00")  # § 6 Abs. 2 EStG
SAMMELPOSTEN_LOWER = Decimal("250.00")  # § 6 Abs. 2a EStG (untere Grenze)
SAMMELPOSTEN_UPPER = Decimal("1000.00")  # § 6 Abs. 2a EStG (obere Grenze)
SAMMELPOSTEN_YEARS = 5


class DepreciationError(ValueError):
    """Ungültige Abschreibungsparameter."""


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(slots=True)
class ScheduleRow:
    """Eine Planzeile: Abschreibung eines Wirtschaftsjahres."""

    year: int
    months: int
    book_value_start: Decimal
    depreciation: Decimal
    book_value_end: Decimal
    cumulative: Decimal
    note: str = ""


@dataclass(slots=True)
class AssetParams:
    """Stammdaten eines Anlageguts, soweit für die AfA-Berechnung nötig."""

    method: str
    acquisition_cost: Decimal
    in_service_date: date
    useful_life_months: int | None = None
    degressive_rate: Decimal | None = None  # Prozentsatz p. a., z. B. 20 = 20 %
    residual_value: Decimal = Decimal("0.00")
    keep_memo_value: bool = False  # Erinnerungswert 1,00 € stehen lassen
    total_units: Decimal | None = None  # Gesamtleistung für Leistungs-AfA
    # Leistung je Wirtschaftsjahr {Jahr: Menge}; nur für ``leistung`` nötig.
    units_per_year: dict[int, Decimal] = field(default_factory=dict)


def floor_value(params: AssetParams) -> Decimal:
    """Untergrenze des Buchwerts: Restwert, mindestens Erinnerungswert."""
    residual = _q(params.residual_value or Decimal("0.00"))
    if params.keep_memo_value and residual < MEMO_VALUE:
        return MEMO_VALUE
    return residual


def max_degressive_rate(
    useful_life_months: int, *, factor: Decimal, cap_percent: Decimal
) -> Decimal:
    """Höchstzulässiger degressiver Satz: ``factor`` × linearer Satz, gedeckelt.

    Die konkreten Werte richten sich nach dem Anschaffungszeitpunkt, z. B.:

    * dauerhaft bis 2007 / Corona-Jahre 2020–2022: 2,5-facher linearer Satz,
      höchstens 25 %.
    * Wachstumschancengesetz 2024/2025: 2,0-facher linearer Satz, höchstens 20 %.

    ``factor`` und ``cap_percent`` sind daher Eingabeparameter.
    """
    if useful_life_months <= 0:
        raise DepreciationError("Nutzungsdauer muss größer 0 sein.")
    linear_rate = Decimal(12 * 100) / Decimal(useful_life_months)
    return min(factor * linear_rate, cap_percent)


def compute_schedule(params: AssetParams) -> list[ScheduleRow]:
    """Erzeugt den Abschreibungsplan für die gewählte Methode."""
    if params.method not in METHODS:
        raise DepreciationError(f"Unbekanntes AfA-Verfahren: {params.method}")
    if params.acquisition_cost <= Decimal("0.00"):
        raise DepreciationError("Anschaffungskosten müssen größer 0 sein.")

    if params.method == MANUELL:
        return []
    if params.method == GWG:
        return _schedule_gwg(params)
    if params.method == SAMMELPOSTEN:
        return _schedule_sammelposten(params)
    if params.method == LEISTUNG:
        return _schedule_leistung(params)
    if params.method == LINEAR:
        return _schedule_linear(params)
    return _schedule_degressive(params)


def _months_in_first_year(in_service: date) -> int:
    """Volle Monate von der Inbetriebnahme bis Jahresende (Monat zählt voll)."""
    return 12 - in_service.month + 1


def _require_useful_life(params: AssetParams) -> int:
    if not params.useful_life_months or params.useful_life_months <= 0:
        raise DepreciationError(
            "Nutzungsdauer (in Monaten) ist für dieses Verfahren erforderlich."
        )
    return params.useful_life_months


def _schedule_linear(params: AssetParams) -> list[ScheduleRow]:
    useful_life = _require_useful_life(params)
    floor = floor_value(params)
    base = params.acquisition_cost - floor
    if base <= Decimal("0.00"):
        raise DepreciationError("Restwert darf die Anschaffungskosten nicht erreichen.")

    monthly = base / Decimal(useful_life)
    rows: list[ScheduleRow] = []
    book_value = params.acquisition_cost
    cumulative = Decimal("0.00")
    remaining_months = useful_life
    year = params.in_service_date.year
    month_cursor = params.in_service_date.month

    while remaining_months > 0:
        months_this_year = min(remaining_months, 12 - month_cursor + 1)
        remaining_months -= months_this_year
        if remaining_months == 0:
            depreciation = book_value - floor  # Rundungsrest ins letzte Jahr
        else:
            depreciation = _q(monthly * Decimal(months_this_year))
        depreciation = min(depreciation, book_value - floor)

        book_value -= depreciation
        cumulative += depreciation
        rows.append(
            ScheduleRow(
                year=year,
                months=months_this_year,
                book_value_start=book_value + depreciation,
                depreciation=depreciation,
                book_value_end=book_value,
                cumulative=cumulative,
            )
        )
        year += 1
        month_cursor = 1
    return rows


def _schedule_degressive(params: AssetParams) -> list[ScheduleRow]:
    useful_life = _require_useful_life(params)
    if not params.degressive_rate or params.degressive_rate <= Decimal("0.00"):
        raise DepreciationError("Für die degressive AfA ist ein Prozentsatz erforderlich.")
    floor = floor_value(params)
    if params.acquisition_cost - floor <= Decimal("0.00"):
        raise DepreciationError("Restwert darf die Anschaffungskosten nicht erreichen.")

    rate = params.degressive_rate / Decimal("100")
    rows: list[ScheduleRow] = []
    book_value = params.acquisition_cost
    cumulative = Decimal("0.00")
    remaining_months = useful_life
    year = params.in_service_date.year
    month_cursor = params.in_service_date.month
    switched = False

    while remaining_months > 0 and book_value > floor:
        months_this_year = min(remaining_months, 12 - month_cursor + 1)
        remaining_life = remaining_months
        remaining_months -= months_this_year

        degressive_amount = _q(
            book_value * rate * Decimal(months_this_year) / Decimal(12)
        )
        # Übergang zur linearen AfA: gleichmäßige Verteilung des Restbuchwerts
        # auf die (noch) verbleibende Nutzungsdauer.
        linear_amount = _q(
            (book_value - floor) / Decimal(remaining_life) * Decimal(months_this_year)
        )
        if switched or linear_amount >= degressive_amount:
            depreciation = linear_amount
            switched = True
            note = "linear (Übergang)"
        else:
            depreciation = degressive_amount
            note = "degressiv"

        if remaining_months == 0:
            depreciation = book_value - floor  # letztes Jahr glatt auf den Floor
        depreciation = min(depreciation, book_value - floor)

        book_value -= depreciation
        cumulative += depreciation
        rows.append(
            ScheduleRow(
                year=year,
                months=months_this_year,
                book_value_start=book_value + depreciation,
                depreciation=depreciation,
                book_value_end=book_value,
                cumulative=cumulative,
                note=note,
            )
        )
        year += 1
        month_cursor = 1
    return rows


def _schedule_leistung(params: AssetParams) -> list[ScheduleRow]:
    if not params.total_units or params.total_units <= Decimal("0.00"):
        raise DepreciationError("Für die Leistungs-AfA ist die Gesamtleistung erforderlich.")
    floor = floor_value(params)
    base = params.acquisition_cost - floor
    if base <= Decimal("0.00"):
        raise DepreciationError("Restwert darf die Anschaffungskosten nicht erreichen.")

    per_unit = base / params.total_units
    rows: list[ScheduleRow] = []
    book_value = params.acquisition_cost
    cumulative = Decimal("0.00")
    for year in sorted(params.units_per_year):
        units = params.units_per_year[year]
        if units <= Decimal("0.00"):
            continue
        depreciation = min(_q(per_unit * units), book_value - floor)
        book_value -= depreciation
        cumulative += depreciation
        rows.append(
            ScheduleRow(
                year=year,
                months=0,
                book_value_start=book_value + depreciation,
                depreciation=depreciation,
                book_value_end=book_value,
                cumulative=cumulative,
                note=f"{units} Einheiten",
            )
        )
        if book_value <= floor:
            break
    return rows


def _schedule_gwg(params: AssetParams) -> list[ScheduleRow]:
    cost = _q(params.acquisition_cost)
    year = params.in_service_date.year
    return [
        ScheduleRow(
            year=year,
            months=0,
            book_value_start=cost,
            depreciation=cost,
            book_value_end=Decimal("0.00"),
            cumulative=cost,
            note="Sofortabschreibung GWG (§ 6 Abs. 2 EStG)",
        )
    ]


def _schedule_sammelposten(params: AssetParams) -> list[ScheduleRow]:
    cost = _q(params.acquisition_cost)
    annual = _q(cost / Decimal(SAMMELPOSTEN_YEARS))
    rows: list[ScheduleRow] = []
    book_value = cost
    cumulative = Decimal("0.00")
    year = params.in_service_date.year
    for index in range(SAMMELPOSTEN_YEARS):
        if index == SAMMELPOSTEN_YEARS - 1:
            depreciation = book_value  # Rundungsrest im letzten Jahr
        else:
            depreciation = annual
        book_value -= depreciation
        cumulative += depreciation
        rows.append(
            ScheduleRow(
                year=year + index,
                months=12,
                book_value_start=book_value + depreciation,
                depreciation=depreciation,
                book_value_end=book_value,
                cumulative=cumulative,
                note="Sammelposten 20 % (§ 6 Abs. 2a EStG)",
            )
        )
    return rows


def depreciation_for_year(params: AssetParams, year: int) -> Decimal:
    """AfA-Betrag eines einzelnen Wirtschaftsjahres laut Plan (0, wenn keine)."""
    for row in compute_schedule(params):
        if row.year == year:
            return row.depreciation
    return Decimal("0.00")
