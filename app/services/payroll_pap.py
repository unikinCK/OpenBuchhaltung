"""PAP-/Payroll-Compliance-Adapter.

Die Lohnsteuer rechnet OpenBuchhaltung nach dem amtlichen Programmablaufplan (PAP)
des BMF, der aus dem veröffentlichten XML erzeugt wird (``app.services.pap``).
Daneben bleibt ein lokaler ``PAYROLL_PAP_COMMAND`` möglich, der per JSON rechnet und
dann Vorrang hat (z. B. ein zertifiziertes Fremdprogramm).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Mapping

from app.services import pap
from domain.models import PayrollEmployee

FEDERAL_STATES = {
    "BW": "Baden-Württemberg",
    "BY": "Bayern",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hessen",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen",
    "NW": "Nordrhein-Westfalen",
    "RP": "Rheinland-Pfalz",
    "SL": "Saarland",
    "SN": "Sachsen",
    "ST": "Sachsen-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thüringen",
}

# Kirchensteuermerkmale laut ELStAM und ihre Kennzahl in der Lohnsteuer-Anmeldung.
CHURCH_DENOMINATIONS: dict[str, tuple[str, str]] = {
    "ev": ("evangelisch", "61"),
    "lt": ("evangelisch-lutherisch", "61"),
    "rf": ("evangelisch-reformiert", "61"),
    "fr": ("französisch-reformiert", "61"),
    "rk": ("römisch-katholisch", "62"),
    "ak": ("alt-katholisch", "63"),
    "is": ("israelitisch", "64"),
    "jd": ("jüdisch", "64"),
}

# Kirchensteuersatz nach dem Land der lohnsteuerlichen Betriebsstätte.
CHURCH_TAX_RATE_LOW_STATES = {"BW", "BY"}
CHURCH_TAX_RATE_LOW = Decimal("0.08")
CHURCH_TAX_RATE_DEFAULT = Decimal("0.09")

CENT = Decimal("0.01")


class PayrollPapError(ValueError):
    """Raised when the PAP command cannot calculate payroll tax."""


@dataclass(frozen=True, slots=True)
class PayrollPapResult:
    wage_tax: Decimal
    church_tax: Decimal
    solidarity_surcharge: Decimal
    version: str
    protocol: str | None = None
    details: dict[str, object] | None = None


def payroll_compliance_readiness(config: Mapping[str, object]) -> dict[str, object]:
    pap_command = str(config.get("PAYROLL_PAP_COMMAND") or "").strip()
    elstam_command = str(config.get("PAYROLL_ELSTAM_COMMAND") or "").strip()
    deuev_command = str(config.get("PAYROLL_DEUEV_COMMAND") or "").strip()
    parameter_version = str(config.get("PAYROLL_PARAMETER_VERSION") or "manual").strip()

    years = pap.supported_years()
    checks = [
        {
            "id": "pap_builtin",
            "label": "Lohnsteuer-PAP (amtlich, integriert)",
            "ok": bool(years),
            "required_for_full_payroll": False,
            "detail": ", ".join(f"PAP {year}" for year in years) or "kein Jahr hinterlegt",
        },
        _command_check(
            "pap_command",
            "PAP command runner",
            pap_command,
            required_for_full_payroll=False,
        ),
        _command_check(
            "elstam_command",
            "ELStAM command runner",
            elstam_command,
            required_for_full_payroll=True,
        ),
        _command_check(
            "deuev_command",
            "DEUEV/SV command runner",
            deuev_command,
            required_for_full_payroll=True,
        ),
        {
            "id": "parameter_version",
            "label": "Payroll parameter version",
            "ok": bool(parameter_version and parameter_version != "manual"),
            "required_for_full_payroll": True,
            "detail": parameter_version or "manual",
        },
    ]
    missing = [
        str(check["id"])
        for check in checks
        if check["required_for_full_payroll"] and not check["ok"]
    ]
    return {
        "full_payroll_ready": not missing,
        "pap_available": bool(years) or bool(checks[1]["ok"]),
        "pap_years": years,
        "parameter_version": parameter_version,
        "checks": checks,
        "missing_requirements": missing,
        "notes": [
            "Lohnsteuer, Solidaritätszuschlag und Kirchensteuer rechnet OpenBuchhaltung "
            "für Mitarbeiter mit Berechnungsart 'pap' nach dem amtlichen PAP des BMF.",
            "Ein konfigurierter PAYROLL_PAP_COMMAND hat Vorrang vor dem integrierten PAP.",
            "Sozialversicherungsbeiträge laufen weiter über die hinterlegten Raten; "
            "ELStAM-Abruf und DEÜV-Meldungen sind nicht enthalten.",
        ],
    }


def calculate_pap_wage_tax(
    *,
    employee: PayrollEmployee,
    gross_pay: Decimal,
    payment_date: date,
    period_label: str,
    config: Mapping[str, object],
) -> PayrollPapResult | None:
    command = str(config.get("PAYROLL_PAP_COMMAND") or "").strip()
    if not command:
        return None
    if not Path(command).exists():
        raise PayrollPapError("PAYROLL_PAP_COMMAND does not exist.")

    payload = {
        "schema_version": "1",
        "period_label": period_label,
        "payment_date": payment_date.isoformat(),
        "gross_pay": str(gross_pay),
        "employee": {
            "employee_number": employee.employee_number,
            "birth_date": employee.birth_date.isoformat() if employee.birth_date else None,
            "tax_id": employee.tax_id,
            "tax_class": employee.tax_class,
            "child_allowances": str(employee.child_allowances),
            "federal_state": employee.federal_state,
            "main_employment": employee.main_employment,
        },
    }
    try:
        completed = subprocess.run(
            [command],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=int(config.get("PAYROLL_PAP_TIMEOUT_SECONDS") or 30),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PayrollPapError("PAYROLL_PAP_COMMAND timed out.") from exc

    if completed.returncode != 0:
        output = (completed.stderr or completed.stdout).strip()
        detail = f": {output}" if output else ""
        raise PayrollPapError(
            f"PAYROLL_PAP_COMMAND failed with exit code {completed.returncode}{detail}"
        )

    try:
        response = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise PayrollPapError("PAYROLL_PAP_COMMAND returned invalid JSON.") from exc

    try:
        return PayrollPapResult(
            wage_tax=Decimal(str(response["wage_tax"])),
            church_tax=Decimal(str(response.get("church_tax") or "0.00")),
            solidarity_surcharge=Decimal(str(response.get("solidarity_surcharge") or "0.00")),
            version=str(response.get("version") or "unknown"),
            protocol=response.get("protocol"),
        )
    except (KeyError, ValueError) as exc:
        raise PayrollPapError("PAYROLL_PAP_COMMAND response is incomplete.") from exc


def church_tax_rate(federal_state: str | None) -> Decimal:
    return (
        CHURCH_TAX_RATE_LOW
        if (federal_state or "").upper() in CHURCH_TAX_RATE_LOW_STATES
        else CHURCH_TAX_RATE_DEFAULT
    )


def church_tax_kennzahl(denomination: str | None) -> str | None:
    if not denomination:
        return None
    entry = CHURCH_DENOMINATIONS.get(denomination)
    return entry[1] if entry else None


def build_pap_inputs(
    *, employee: PayrollEmployee, gross_pay: Decimal, period_label: str
) -> dict[str, object]:
    """PAP-Eingaben für einen Monatslohn aus den Mitarbeiter-Stammdaten."""
    year, month = (int(part) for part in period_label.split("-", 1))
    period_end = _month_end(year, month)
    missing = []
    if employee.birth_date is None:
        missing.append("Geburtsdatum")
    if not employee.federal_state:
        missing.append("Bundesland")
    if missing:
        raise PayrollPapError(
            f"Für die Lohnsteuer nach PAP fehlen bei Mitarbeiter {employee.employee_number}: "
            + ", ".join(missing)
            + "."
        )
    tax_class = int(employee.tax_class)
    private = bool(employee.private_health_insurance)
    children = int(employee.care_insurance_children or 0)
    inputs: dict[str, object] = {
        "LZZ": pap.LZZ_MONTH,
        "RE4": _cents(gross_pay),
        "STKL": tax_class,
        "ZKF": Decimal(employee.child_allowances) if tax_class <= 4 else Decimal("0"),
        "R": 1 if employee.church_denomination else 0,
        "KRV": 0 if employee.pension_insurance_compulsory else 1,
        "ALV": 0 if employee.unemployment_insurance_compulsory else 1,
        "PKV": 1 if private else 0,
        "KVZ": Decimal(employee.health_insurance_additional_rate),
        "PVS": 1 if employee.federal_state == "SN" else 0,
        "PVZ": 1 if (children == 0 and _age_at(employee.birth_date, period_end) >= 23) else 0,
        "PVA": Decimal(min(max(children - 1, 0), 4)),
        "LZZFREIB": _cents(employee.monthly_tax_allowance or Decimal("0")),
        "LZZHINZU": _cents(employee.monthly_tax_addition or Decimal("0")),
    }
    if private:
        inputs["PKPV"] = _cents(employee.private_health_premium or Decimal("0"))
        inputs["PKPVAGZ"] = _cents(employee.private_health_employer_subsidy or Decimal("0"))
    if tax_class == 4 and employee.tax_factor:
        inputs["af"] = 1
        inputs["f"] = float(employee.tax_factor)
    else:
        inputs["af"] = 0
    completed_64 = _day_before_birthday(employee.birth_date, 64)
    if completed_64 < date(year, 1, 1):
        inputs["ALTER1"] = 1
        inputs["AJAHR"] = completed_64.year + 1
    return inputs


def calculate_builtin_wage_tax(
    *, employee: PayrollEmployee, gross_pay: Decimal, period_label: str
) -> PayrollPapResult:
    """Lohnsteuer, Soli und Kirchensteuer eines Monatslohns nach dem integrierten PAP."""
    year = int(period_label[:4])
    inputs = build_pap_inputs(employee=employee, gross_pay=gross_pay, period_label=period_label)
    try:
        result = pap.calculate(year, inputs)
    except pap.PapNotAvailableError as exc:
        raise PayrollPapError(str(exc)) from exc
    church_tax = Decimal("0.00")
    rate = None
    if employee.church_denomination:
        rate = church_tax_rate(employee.federal_state)
        church_tax = (result.church_tax_base * rate).quantize(CENT, rounding=ROUND_DOWN)
    return PayrollPapResult(
        wage_tax=result.wage_tax,
        church_tax=church_tax,
        solidarity_surcharge=result.solidarity_surcharge,
        version=f"{result.pap_name} {result.pap_version}",
        details={
            "year": result.year,
            "source_sha256": result.source_sha256,
            "inputs": result.inputs,
            "outputs": result.outputs,
            "church_tax_base": str(result.church_tax_base),
            "church_tax_rate": str(rate) if rate is not None else None,
        },
    )


def _cents(amount: Decimal) -> Decimal:
    return (Decimal(amount) * 100).quantize(Decimal("1"))


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _day_before_birthday(birth_date: date, age: int) -> date:
    """Tag, mit dessen Ablauf das Lebensjahr ``age`` vollendet ist (§ 108 BGB, § 187 BGB)."""
    try:
        birthday = birth_date.replace(year=birth_date.year + age)
    except ValueError:  # 29. Februar
        birthday = date(birth_date.year + age, 3, 1)
    return birthday - timedelta(days=1)


def _age_at(birth_date: date, day: date) -> int:
    years = day.year - birth_date.year
    if (day.month, day.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _command_check(
    check_id: str,
    label: str,
    command: str,
    *,
    required_for_full_payroll: bool,
) -> dict[str, object]:
    exists = bool(command and Path(command).exists())
    return {
        "id": check_id,
        "label": label,
        "ok": exists,
        "required_for_full_payroll": required_for_full_payroll,
        "detail": command or "not configured",
    }
