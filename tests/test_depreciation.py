from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domain.services.depreciation import (
    AssetParams,
    DepreciationError,
    compute_schedule,
    depreciation_for_year,
    max_degressive_rate,
)


def _sum(rows) -> Decimal:
    return sum((row.depreciation for row in rows), Decimal("0.00"))


def test_linear_full_year_start() -> None:
    rows = compute_schedule(
        AssetParams(
            method="linear",
            acquisition_cost=Decimal("12000.00"),
            in_service_date=date(2026, 1, 1),
            useful_life_months=60,
        )
    )
    assert len(rows) == 5
    assert all(row.depreciation == Decimal("2400.00") for row in rows)
    assert rows[0].year == 2026
    assert rows[-1].book_value_end == Decimal("0.00")
    assert _sum(rows) == Decimal("12000.00")


def test_linear_pro_rata_temporis_first_year() -> None:
    # Inbetriebnahme im Oktober -> nur 3 Monate im ersten Jahr (§ 7 Abs. 1 S. 4 EStG).
    rows = compute_schedule(
        AssetParams(
            method="linear",
            acquisition_cost=Decimal("12000.00"),
            in_service_date=date(2026, 10, 1),
            useful_life_months=60,
        )
    )
    assert rows[0].year == 2026
    assert rows[0].months == 3
    assert rows[0].depreciation == Decimal("600.00")
    assert len(rows) == 6  # verteilt sich auf sechs Kalenderjahre
    assert rows[-1].book_value_end == Decimal("0.00")
    assert _sum(rows) == Decimal("12000.00")


def test_linear_with_residual_value() -> None:
    rows = compute_schedule(
        AssetParams(
            method="linear",
            acquisition_cost=Decimal("10000.00"),
            in_service_date=date(2026, 1, 1),
            useful_life_months=48,
            residual_value=Decimal("1000.00"),
        )
    )
    assert rows[-1].book_value_end == Decimal("1000.00")
    assert _sum(rows) == Decimal("9000.00")


def test_linear_keeps_memo_value() -> None:
    rows = compute_schedule(
        AssetParams(
            method="linear",
            acquisition_cost=Decimal("6000.00"),
            in_service_date=date(2026, 1, 1),
            useful_life_months=36,
            keep_memo_value=True,
        )
    )
    assert rows[-1].book_value_end == Decimal("1.00")


def test_degressive_switches_to_linear_and_fully_depreciates() -> None:
    rows = compute_schedule(
        AssetParams(
            method="degressive",
            acquisition_cost=Decimal("10000.00"),
            in_service_date=date(2026, 1, 1),
            useful_life_months=120,
            degressive_rate=Decimal("20"),
        )
    )
    assert len(rows) == 10
    # Erstes Jahr: 20 % vom Buchwert.
    assert rows[0].depreciation == Decimal("2000.00")
    assert rows[0].note == "degressiv"
    # Beträge fallen erst degressiv, ab dem Übergang bleiben sie konstant (linear).
    assert any(row.note == "linear (Übergang)" for row in rows)
    assert rows[-1].book_value_end == Decimal("0.00")
    assert _sum(rows) == Decimal("10000.00")


def test_degressive_pro_rata_first_year() -> None:
    rows = compute_schedule(
        AssetParams(
            method="degressive",
            acquisition_cost=Decimal("10000.00"),
            in_service_date=date(2026, 7, 1),
            useful_life_months=120,
            degressive_rate=Decimal("20"),
        )
    )
    # 6 Monate -> 20 % * 6/12 = 10 % = 1000.
    assert rows[0].depreciation == Decimal("1000.00")
    assert rows[-1].book_value_end == Decimal("0.00")


def test_leistungs_afa_by_units() -> None:
    rows = compute_schedule(
        AssetParams(
            method="leistung",
            acquisition_cost=Decimal("100000.00"),
            in_service_date=date(2026, 1, 1),
            total_units=Decimal("100000"),
            units_per_year={
                2026: Decimal("20000"),
                2027: Decimal("30000"),
                2028: Decimal("50000"),
            },
        )
    )
    assert [row.depreciation for row in rows] == [
        Decimal("20000.00"),
        Decimal("30000.00"),
        Decimal("50000.00"),
    ]
    assert rows[-1].book_value_end == Decimal("0.00")


def test_gwg_immediate_write_off() -> None:
    rows = compute_schedule(
        AssetParams(
            method="gwg",
            acquisition_cost=Decimal("800.00"),
            in_service_date=date(2026, 5, 1),
        )
    )
    assert len(rows) == 1
    assert rows[0].depreciation == Decimal("800.00")
    assert rows[0].book_value_end == Decimal("0.00")


def test_sammelposten_five_years() -> None:
    rows = compute_schedule(
        AssetParams(
            method="sammelposten",
            acquisition_cost=Decimal("1000.00"),
            in_service_date=date(2026, 11, 1),
        )
    )
    assert len(rows) == 5
    # Kein Monatsanteil: volle 20 % auch im Zugangsjahr.
    assert all(row.depreciation == Decimal("200.00") for row in rows)
    assert rows[0].year == 2026
    assert rows[-1].year == 2030
    assert rows[-1].book_value_end == Decimal("0.00")


def test_manuell_has_no_schedule() -> None:
    rows = compute_schedule(
        AssetParams(
            method="manuell",
            acquisition_cost=Decimal("5000.00"),
            in_service_date=date(2026, 1, 1),
        )
    )
    assert rows == []


def test_depreciation_for_year_lookup() -> None:
    params = AssetParams(
        method="linear",
        acquisition_cost=Decimal("12000.00"),
        in_service_date=date(2026, 1, 1),
        useful_life_months=60,
    )
    assert depreciation_for_year(params, 2027) == Decimal("2400.00")
    assert depreciation_for_year(params, 2099) == Decimal("0.00")


def test_max_degressive_rate_capped() -> None:
    # 5 Jahre -> linear 20 %; Faktor 2,0, Deckel 20 % -> 20 %.
    assert max_degressive_rate(60, factor=Decimal("2.0"), cap_percent=Decimal("20")) == Decimal(
        "20"
    )
    # 10 Jahre -> linear 10 %; Faktor 2,5, Deckel 25 % -> 25 % vom Faktor, aber Deckel 25.
    assert max_degressive_rate(120, factor=Decimal("2.5"), cap_percent=Decimal("25")) == Decimal(
        "25"
    )


def test_invalid_method_rejected() -> None:
    with pytest.raises(DepreciationError):
        compute_schedule(
            AssetParams(
                method="unbekannt",
                acquisition_cost=Decimal("1000.00"),
                in_service_date=date(2026, 1, 1),
            )
        )


def test_linear_requires_useful_life() -> None:
    with pytest.raises(DepreciationError):
        compute_schedule(
            AssetParams(
                method="linear",
                acquisition_cost=Decimal("1000.00"),
                in_service_date=date(2026, 1, 1),
            )
        )
