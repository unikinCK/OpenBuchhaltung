"""PAP-/Payroll-Compliance-Adapter.

OpenBuchhaltung implementiert hier bewusst keinen frei erfundenen amtlichen
Programmablaufplan. Stattdessen kapselt dieser Adapter einen lokalen
``PAYROLL_PAP_COMMAND``. Dieser Command kann ein amtlicher oder zertifizierter
PAP-Wrapper sein und erhaelt/antwortet per JSON.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from domain.models import PayrollEmployee


class PayrollPapError(ValueError):
    """Raised when the PAP command cannot calculate payroll tax."""


@dataclass(frozen=True, slots=True)
class PayrollPapResult:
    wage_tax: Decimal
    church_tax: Decimal
    solidarity_surcharge: Decimal
    version: str
    protocol: str | None = None


def payroll_compliance_readiness(config: Mapping[str, object]) -> dict[str, object]:
    pap_command = str(config.get("PAYROLL_PAP_COMMAND") or "").strip()
    elstam_command = str(config.get("PAYROLL_ELSTAM_COMMAND") or "").strip()
    deuev_command = str(config.get("PAYROLL_DEUEV_COMMAND") or "").strip()
    parameter_version = str(config.get("PAYROLL_PARAMETER_VERSION") or "manual").strip()

    checks = [
        _command_check(
            "pap_command",
            "PAP command runner",
            pap_command,
            required_for_full_payroll=True,
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
        "pap_available": checks[0]["ok"],
        "parameter_version": parameter_version,
        "checks": checks,
        "missing_requirements": missing,
        "notes": [
            "Amtliche Produktivnutzung erfordert ELSTER-/ELStAM-Zugang, "
            "ITSG-Systempruefung und laufend gepflegte Parameter.",
            "Ohne PAYROLL_PAP_COMMAND nutzt OpenBuchhaltung die manuell "
            "hinterlegten Abzugsraten des Payroll-MVP.",
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
