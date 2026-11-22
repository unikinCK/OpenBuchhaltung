"""Lohnsteuer nach amtlichem PAP und Lohnsteuer-Anmeldung."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.services import pap
from app.services.pap.lohnsteuer2026 import SOURCE_SHA256, Lohnsteuer2026
from app.services.pap.runtime import BD, PapArithmeticError
from app.services.payroll import (
    PayrollError,
    PayrollRunInput,
    calculate_wage_tax_preview,
    create_payroll_employee,
    create_payroll_run,
    post_payroll_run,
    update_payroll_employee,
)
from app.services.payroll_pap import build_pap_inputs, payroll_compliance_readiness
from app.services.wage_tax_returns import (
    WageTaxReturnError,
    compute_wage_tax_return,
    elster_period_code,
    save_wage_tax_return,
    wage_tax_due_date,
    wage_tax_period_bounds,
)
from domain.models import AuditLog, Base, PayrollEmployee, PayrollRun, WageTaxReturn
from tests.test_payroll import _create_test_app, _employee_input, _seed

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = json.loads((ROOT / "tests/fixtures/pap/bmf-2026.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURES, ids=lambda case: json.dumps(case["inputs"]))
def test_pap_2026_matches_bmf_reference_values(case) -> None:
    outputs = Lohnsteuer2026(**case["inputs"]).calculate()
    assert {key: outputs[key] for key in case["expected"]} == case["expected"]


def test_generated_module_matches_official_xml() -> None:
    import hashlib

    from tools.pap_codegen import generate

    xml = ROOT / "data/pap/Lohnsteuer2026.xml"
    assert hashlib.sha256(xml.read_bytes()).hexdigest() == SOURCE_SHA256
    generated = generate(xml)
    committed = (ROOT / "app/services/pap/lohnsteuer2026.py").read_text(encoding="utf-8")
    assert generated == committed


def test_bigdecimal_runtime_follows_java_semantics() -> None:
    assert BD.valueOf(0.07).value == Decimal("0.07")
    assert BD(10).divide(BD(4)).value == Decimal("2.5")
    with pytest.raises(PapArithmeticError):
        BD(10).divide(BD(3))
    assert BD(10).divide(BD(3), 2, BD.ROUND_DOWN).value == Decimal("3.33")
    assert BD(10).divide(BD(3), 0, BD.ROUND_UP).value == Decimal("4")
    assert BD("1.005").setScale(2, BD.ROUND_DOWN).value == Decimal("1.00")
    assert BD(-1).compareTo(BD.ZERO) == -1
    assert BD("12.9").longValue() == 12


def test_unknown_year_is_reported() -> None:
    with pytest.raises(pap.PapNotAvailableError, match="2025"):
        pap.calculate(2025, {"LZZ": 2, "RE4": 100000, "STKL": 1})


def _pap_employee(**overrides) -> PayrollEmployee:
    values = {
        "employee_number": "P-1",
        "birth_date": date(1990, 6, 15),
        "federal_state": "NW",
        "tax_class": 1,
        "child_allowances": Decimal("0"),
        "church_denomination": None,
        "health_insurance_additional_rate": Decimal("2.50"),
        "private_health_insurance": False,
        "private_health_premium": Decimal("0"),
        "private_health_employer_subsidy": Decimal("0"),
        "care_insurance_children": 0,
        "pension_insurance_compulsory": True,
        "unemployment_insurance_compulsory": True,
        "monthly_tax_allowance": Decimal("0"),
        "monthly_tax_addition": Decimal("0"),
        "tax_factor": None,
        "tax_calculation": "pap",
    }
    values.update(overrides)
    return PayrollEmployee(**values)


def test_pap_inputs_from_employee_master_data() -> None:
    inputs = build_pap_inputs(
        employee=_pap_employee(), gross_pay=Decimal("5000.00"), period_label="2026-03"
    )
    assert inputs["RE4"] == Decimal("500000")
    assert inputs["PVZ"] == 1  # kinderlos und älter als 23
    assert inputs["PVA"] == Decimal("0")
    assert inputs["R"] == 0
    assert "ALTER1" not in inputs

    family = build_pap_inputs(
        employee=_pap_employee(
            care_insurance_children=3,
            church_denomination="rk",
            federal_state="SN",
            tax_class=4,
            tax_factor=Decimal("0.912"),
            child_allowances=Decimal("1.5"),
        ),
        gross_pay=Decimal("3000.00"),
        period_label="2026-03",
    )
    assert family["PVZ"] == 0
    assert family["PVA"] == Decimal("2")
    assert family["PVS"] == 1
    assert family["R"] == 1
    assert family["af"] == 1 and family["f"] == 0.912
    assert family["ZKF"] == Decimal("1.5")

    # 64. Lebensjahr mit Ablauf des 31.12.2025 vollendet → Altersentlastungsbetrag ab 2026.
    senior = build_pap_inputs(
        employee=_pap_employee(birth_date=date(1962, 1, 1)),
        gross_pay=Decimal("3000.00"),
        period_label="2026-01",
    )
    assert senior["ALTER1"] == 1 and senior["AJAHR"] == 2026
    younger = build_pap_inputs(
        employee=_pap_employee(birth_date=date(1962, 1, 2)),
        gross_pay=Decimal("3000.00"),
        period_label="2026-01",
    )
    assert "ALTER1" not in younger


def test_wage_tax_preview_matches_bmf_case_and_adds_church_tax() -> None:
    # BMF-Prüffall: 5.000 € Monatslohn, Steuerklasse I, Zusatzbeitrag 2,5 %, PV-Zuschlag.
    base = {
        "gross_monthly_salary": Decimal("5000.00"),
        "tax_class": 1,
        "birth_date": date(1990, 6, 15),
        "federal_state": "NW",
        "health_insurance_additional_rate": Decimal("2.50"),
    }
    result = calculate_wage_tax_preview(values=base, period_label="2026-03")
    assert result["wage_tax"] == "785.83"
    assert result["solidarity_surcharge"] == "0.00"
    assert result["church_tax"] == "0.00"

    church = calculate_wage_tax_preview(
        values={**base, "church_denomination": "ev"}, period_label="2026-03"
    )
    assert church["church_tax"] == "70.72"  # 9 % von 785,83 €, abgerundet
    bavaria = calculate_wage_tax_preview(
        values={**base, "church_denomination": "rk", "federal_state": "BY"},
        period_label="2026-03",
    )
    assert bavaria["church_tax"] == "62.86"  # 8 % in Bayern
    with pytest.raises(PayrollError, match="2027"):
        calculate_wage_tax_preview(values=base, period_label="2027-01")


def _pap_seed(session: Session):
    company, accounts = _seed(session)
    payload = _employee_input(company)
    payload.tax_calculation = "pap"
    payload.church_denomination = "ev"
    payload.health_insurance_additional_rate = Decimal("2.50")
    payload.gross_monthly_salary = Decimal("5000.00")
    first = create_payroll_employee(session=session, payload=payload)
    second_payload = _employee_input(company)
    second_payload.employee_number = "P-002"
    second_payload.first_name = "Grace"
    second_payload.church_denomination = "rk"
    second_payload.wage_tax_rate = Decimal("0.10")
    second_payload.church_tax_rate = Decimal("0.009")
    second_payload.solidarity_surcharge_rate = Decimal("0.00")
    second = create_payroll_employee(session=session, payload=second_payload)
    return company, accounts, first, second


def _run(session, company, period, payment, employee_ids=None, post=True):
    run = create_payroll_run(
        session=session,
        payload=PayrollRunInput(
            company_id=company.id,
            period_label=period,
            payment_date=payment,
            employee_ids=employee_ids,
            changed_by="pytest",
        ),
    )
    if post:
        run = post_payroll_run(session=session, payroll_run_id=run.id, changed_by="pytest")
    return run


def test_payroll_run_with_pap_and_wage_tax_return() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        company, _accounts, first, second = _pap_seed(session)

        march = _run(session, company, "2026-03", date(2026, 3, 31))
        line = next(item for item in march.lines if item.employee_id == first.id)
        assert line.wage_tax == Decimal("785.83")
        assert line.church_tax == Decimal("70.72")
        assert line.church_denomination == "ev"
        assert line.calculation["tax"]["mode"] == "pap"
        assert line.calculation["tax"]["pap"]["source_sha256"] == SOURCE_SHA256
        manual = next(item for item in march.lines if item.employee_id == second.id)
        assert manual.wage_tax == Decimal("400.00")
        assert manual.church_tax == Decimal("36.00")
        assert manual.calculation["tax"]["mode"] == "manual_rates"

        # Lohnlauf Februar erst im März gezahlt: zählt zur Anmeldung März.
        _run(session, company, "2026-02", date(2026, 3, 25), employee_ids=[second.id])
        # Entwurf im Zeitraum fehlt und wird gemeldet.
        _run(session, company, "2026-04", date(2026, 3, 30), employee_ids=[second.id], post=False)

        result = compute_wage_tax_return(
            session=session, company_id=company.id, period_label="2026-03"
        )
        amounts = {row.kennzahl: Decimal(row.amount) for row in result.rows}
        assert amounts["86"] == 2
        assert amounts["42"] == Decimal("1585.83")
        assert amounts["42_lfdJahr"] == Decimal("1585.83")
        assert amounts["48"] == amounts["42"]
        assert amounts["61"] == Decimal("70.72")
        assert amounts["62"] == Decimal("72.00")
        assert amounts["83"] == Decimal("1728.55")
        assert result.due_date == date(2026, 4, 10)
        assert result.liability["difference"] == "0.00"
        assert not result.issues
        assert any("2026-04" in warning and "nicht" in warning for warning in result.warnings)
        assert any("2026-02" in warning for warning in result.warnings)

        saved = save_wage_tax_return(
            session=session, company_id=company.id, period_label="2026-03", changed_by="pytest"
        )
        assert saved.correction_number == 0
        assert {row["kennzahl"] for row in saved.kennzahlen} >= {"86", "42", "83"}
        with pytest.raises(WageTaxReturnError, match="Berichtigung"):
            save_wage_tax_return(
                session=session,
                company_id=company.id,
                period_label="2026-03",
                changed_by="pytest",
            )
        corrected = save_wage_tax_return(
            session=session,
            company_id=company.id,
            period_label="2026-03",
            changed_by="pytest",
            correction=True,
        )
        assert corrected.correction_number == 1
        assert session.execute(select(WageTaxReturn)).scalars().all()
        assert session.execute(
            select(AuditLog).where(AuditLog.entity_type == "wage_tax_return")
        ).scalars().all()


def test_wage_tax_return_splits_previous_year_and_blocks_missing_denomination() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        company, _accounts, _first, second = _pap_seed(session)
        # Dezember 2025 (manuelle Raten, kein PAP 2025) im Januar 2026 gezahlt.
        _run(session, company, "2025-12", date(2026, 1, 9), employee_ids=[second.id])
        result = compute_wage_tax_return(
            session=session, company_id=company.id, period_label="2026-Q1"
        )
        amounts = {row.kennzahl: Decimal(row.amount) for row in result.rows}
        assert amounts["42_Vorjahr"] == Decimal("400.00")
        assert "42_lfdJahr" not in amounts
        assert amounts["62_Vorjahr"] == Decimal("36.00")
        assert elster_period_code(result.period_label) == "41"

        update_payroll_employee(
            session=session,
            employee_id=second.id,
            changes={"church_denomination": None},
            changed_by="pytest",
        )
        _run(session, company, "2026-01", date(2026, 1, 30), employee_ids=[second.id])
        blocked = compute_wage_tax_return(
            session=session, company_id=company.id, period_label="2026-01"
        )
        assert blocked.issues
        with pytest.raises(WageTaxReturnError, match="Kirchensteuermerkmal"):
            save_wage_tax_return(
                session=session,
                company_id=company.id,
                period_label="2026-01",
                changed_by="pytest",
            )


def test_period_helpers() -> None:
    assert elster_period_code("2026-05") == "05"
    assert elster_period_code("2026") == "19"
    assert wage_tax_due_date(date(2026, 1, 31)) == date(2026, 2, 10)
    # 10.10.2026 ist ein Samstag → Montag, 12.10.2026.
    assert wage_tax_due_date(date(2026, 9, 30)) == date(2026, 10, 12)
    with pytest.raises(WageTaxReturnError):
        wage_tax_period_bounds("2026-H1")


def test_employee_update_validates_and_audits() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        company, _accounts = _seed(session)
        employee = create_payroll_employee(session=session, payload=_employee_input(company))
        assert employee.tax_calculation == "manual"
        updated = update_payroll_employee(
            session=session,
            employee_id=employee.id,
            changes={"tax_calculation": "pap", "church_denomination": "EV", "tax_class": 3},
            changed_by="pytest",
        )
        assert updated.tax_calculation == "pap"
        assert updated.church_denomination == "ev"
        assert updated.tax_class == 3
        entry = session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "payroll_employee", AuditLog.action == "updated"
            )
        ).scalar_one()
        assert entry.payload["changes"]["tax_class"] == {"before": 1, "after": 3}

        with pytest.raises(PayrollError, match="Kirchensteuermerkmal"):
            update_payroll_employee(
                session=session, employee_id=employee.id, changes={"church_denomination": "xx"}
            )
        session.rollback()
        with pytest.raises(PayrollError, match="Faktor"):
            update_payroll_employee(
                session=session, employee_id=employee.id, changes={"tax_factor": Decimal("0.9")}
            )
        session.rollback()
        with pytest.raises(PayrollError, match="Bundesland"):
            update_payroll_employee(
                session=session, employee_id=employee.id, changes={"federal_state": None}
            )


def test_readiness_reports_builtin_pap() -> None:
    readiness = payroll_compliance_readiness({})
    assert readiness["pap_available"] is True
    assert readiness["pap_years"] == [2026]
    assert readiness["full_payroll_ready"] is False


def test_wage_tax_api_flow(tmp_path: Path) -> None:
    app, company_id, _ = _create_test_app(tmp_path)
    client = app.test_client()
    created = client.post(
        "/api/v1/payroll/employees",
        json={
            "company_id": company_id,
            "employee_number": "P-010",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "employment_start": "2026-01-01",
            "birth_date": "1990-06-15",
            "tax_class": 1,
            "federal_state": "NW",
            "gross_monthly_salary": "5000.00",
            "tax_calculation": "manual",
            "employee_social_security_rate": "0.20",
            "employer_social_security_rate": "0.20",
            "wage_expense_account_code": "4120",
            "employer_social_security_expense_account_code": "4130",
            "payroll_liability_account_code": "1740",
            "wage_tax_liability_account_code": "1741",
            "social_security_liability_account_code": "1742",
        },
    )
    assert created.status_code == 201, created.get_json()
    employee_id = created.get_json()["id"]

    patched = client.patch(
        f"/api/v1/payroll/employees/{employee_id}",
        json={
            "tax_calculation": "pap",
            "church_denomination": "ev",
            "health_insurance_additional_rate": "2.50",
        },
    )
    assert patched.status_code == 200, patched.get_json()
    assert patched.get_json()["tax_calculation"] == "pap"
    assert client.patch(
        f"/api/v1/payroll/employees/{employee_id}", json={"unknown": 1}
    ).status_code == 422

    calc = client.post(
        "/api/v1/payroll/wage-tax/calculate",
        json={
            "period_label": "2026-03",
            "gross_monthly_salary": "5000.00",
            "tax_class": 1,
            "birth_date": "1990-06-15",
            "federal_state": "NW",
            "health_insurance_additional_rate": "2.50",
        },
    )
    assert calc.status_code == 200, calc.get_json()
    assert calc.get_json()["wage_tax"] == "785.83"

    run = client.post(
        "/api/v1/payroll/runs",
        json={
            "company_id": company_id,
            "period_label": "2026-03",
            "payment_date": "2026-03-31",
            "auto_post": True,
        },
    )
    assert run.status_code == 201, run.get_json()
    assert run.get_json()["lines"][0]["church_denomination"] == "ev"

    preview = client.get(
        "/api/v1/payroll/wage-tax-returns/preview",
        query_string={"company_id": company_id, "period_label": "2026-03"},
    )
    assert preview.status_code == 200
    body = preview.get_json()
    kennzahlen = {row["kennzahl"]: row["amount"] for row in body["kennzahlen"]}
    assert kennzahlen["42"] == "785.83"
    assert kennzahlen["61"] == "70.72"
    assert kennzahlen["83"] == "856.55"
    assert body["due_date"] == "2026-04-10"
    assert body["elster_period"] == "03"

    saved = client.post(
        "/api/v1/payroll/wage-tax-returns",
        json={"company_id": company_id, "period_label": "2026-03"},
    )
    assert saved.status_code == 201, saved.get_json()
    assert saved.get_json()["is_correction"] is False
    again = client.post(
        "/api/v1/payroll/wage-tax-returns",
        json={"company_id": company_id, "period_label": "2026-03"},
    )
    assert again.status_code == 422
    listing = client.get(
        "/api/v1/payroll/wage-tax-returns", query_string={"company_id": company_id}
    )
    assert listing.status_code == 200
    assert len(listing.get_json()["wage_tax_returns"]) == 1


def test_wage_tax_ui_flow(tmp_path: Path) -> None:
    app, company_id, account_ids = _create_test_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})

    page = client.get("/lohn", query_string={"company_id": company_id})
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Lohnsteuer-Anmeldung" in html and "Lohnsteuer-Rechner" in html

    calc = client.post(
        "/lohn/lohnsteuer-rechner",
        data={
            "company_id": str(company_id),
            "period_label": "2026-03",
            "gross_monthly_salary": "5.000,00",
            "tax_class": "1",
            "birth_date": "1990-06-15",
            "federal_state": "NW",
            "church_denomination": "ev",
            "health_insurance_additional_rate": "2,50",
            "care_insurance_children": "0",
        },
    )
    assert calc.status_code == 200
    calc_html = calc.get_data(as_text=True)
    assert "785,83" in calc_html and "70,72" in calc_html

    created = client.post(
        "/lohn/mitarbeiter",
        data={
            "company_id": str(company_id),
            "employee_number": "P-020",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "employment_start": "2026-01-01",
            "birth_date": "1990-06-15",
            "tax_class": "1",
            "child_allowances": "0.0",
            "federal_state": "NW",
            "tax_calculation": "manual",
            "church_denomination": "",
            "health_insurance_additional_rate": "2.90",
            "gross_monthly_salary": "5000.00",
            "employee_social_security_rate": "0.20",
            "employer_social_security_rate": "0.20",
            "wage_expense_account_id": str(account_ids["4120"]),
            "employer_social_security_expense_account_id": str(account_ids["4130"]),
            "payroll_liability_account_id": str(account_ids["1740"]),
            "wage_tax_liability_account_id": str(account_ids["1741"]),
            "social_security_liability_account_id": str(account_ids["1742"]),
        },
    )
    assert created.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        employee = session.execute(select(PayrollEmployee)).scalar_one()
        employee_id = employee.id

    edit = client.get(f"/lohn/mitarbeiter/{employee_id}")
    assert edit.status_code == 200
    assert "Lohnsteuer und Sozialversicherung" in edit.get_data(as_text=True)
    form = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "employment_start": "2026-01-01",
        "status": "active",
        "birth_date": "1990-06-15",
        "tax_class": "1",
        "child_allowances": "0.0",
        "federal_state": "NW",
        "main_employment": "1",
        "gross_monthly_salary": "5000.00",
        "tax_calculation": "pap",
        "church_denomination": "ev",
        "health_insurance_additional_rate": "2,50",
        "care_insurance_children": "0",
        "private_health_insurance": "0",
        "pension_insurance_compulsory": "1",
        "unemployment_insurance_compulsory": "1",
        "wage_tax_rate": "0",
        "church_tax_rate": "0",
        "solidarity_surcharge_rate": "0",
        "employee_social_security_rate": "0.20",
        "employer_social_security_rate": "0.20",
        "wage_expense_account_id": str(account_ids["4120"]),
        "employer_social_security_expense_account_id": str(account_ids["4130"]),
        "payroll_liability_account_id": str(account_ids["1740"]),
        "wage_tax_liability_account_id": str(account_ids["1741"]),
        "social_security_liability_account_id": str(account_ids["1742"]),
    }
    saved = client.post(f"/lohn/mitarbeiter/{employee_id}", data=form)
    assert saved.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        employee = session.get(PayrollEmployee, employee_id)
        assert employee.tax_calculation == "pap"
        assert employee.health_insurance_additional_rate == Decimal("2.50")

    client.post(
        "/lohn/laeufe",
        data={
            "company_id": str(company_id),
            "period_label": "2026-03",
            "payment_date": "2026-03-31",
        },
    )
    with app.extensions["db_session_factory"]() as session:
        run_id = session.execute(select(PayrollRun)).scalar_one().id
    client.post(f"/lohn/laeufe/{run_id}/buchen")

    preview = client.get(
        "/lohn", query_string={"company_id": company_id, "lsta_period": "2026-03"}
    )
    preview_html = preview.get_data(as_text=True)
    assert "856,55" in preview_html and "10.04.2026" in preview_html

    stored = client.post(
        "/lohn/lohnsteuer-anmeldung",
        data={"company_id": str(company_id), "period_label": "2026-03"},
    )
    assert stored.status_code == 302
    with app.extensions["db_session_factory"]() as session:
        assert session.execute(select(WageTaxReturn)).scalar_one().period_label == "2026-03"
    listing = client.get("/lohn", query_string={"company_id": company_id})
    assert "Erstanmeldung" in listing.get_data(as_text=True)
