"""ELSTER-Grundlage: Payloads, Transportkante und Übermittlungsprotokolle.

Der Mock-Transport ist deterministisch und nur für die Testumgebung. Der ERiC-Transport übergibt
den amtlichen ElsterXML-Datensatz (``app.services.elster_xml``) an einen eigenen Prozess – den
integrierten Runner ``app.services.eric_runner`` oder einen Befehl aus ``ELSTER_ERIC_COMMAND`` –,
damit die ERiC-Bibliothek außerhalb der Webprozesse läuft (ADR-011).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.elster_certificates import (
    CertificateMaterial,
    ElsterCertificateError,
    company_certificate_material,
    get_company_elster_certificate,
    serialize_company_elster_certificate,
)
from app.services.elster_xml import (
    ElsterXmlError,
    ElsterXmlOptions,
    build_ustva_elster_xml,
    ustva_datenart_version,
    ustva_period,
)
from app.services.push import notify_elster_submission
from app.services.vat_returns import (
    VAT_RETURN_KIND_ANNUAL,
    vat_return_display_name,
    vat_return_kind_from_label,
)
from app.version import get_version
from domain.models import ElsterSubmission, VatReturn

ELSTER_ENVIRONMENTS = {"test", "production"}
ELSTER_PAYLOAD_ROOT = "OpenBuchhaltungElster"
ELSTER_PAYLOAD_VERSION = "1"
ELSTER_PROCEDURE_USTVA = "ustva"
ELSTER_PROCEDURE_VAT_ANNUAL = "ust_jahreserklaerung"
ELSTER_STATUSES = {"created", "transmitted", "failed"}
ELSTER_TRANSPORTS = {"mock", "eric"}
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_ERIC_RUNNER = "app.services.eric_runner"


class ElsterError(ValueError):
    """Raised when an ELSTER submission cannot be created or transmitted."""


@dataclass(frozen=True, slots=True)
class ElsterTransportResult:
    status: str
    transfer_ticket: str
    response_protocol: str


class ElsterTransport(Protocol):
    name: str

    def transmit(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn
    ) -> ElsterTransportResult:
        """Transmit a prepared payload and return the normalized protocol result."""


@dataclass(frozen=True, slots=True)
class MockElsterTransport:
    name: str = "mock"

    def transmit(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn
    ) -> ElsterTransportResult:
        del payload_xml
        procedure = vat_return_elster_procedure(vat_return)
        prefix = (
            "MOCK-UST-JAHR"
            if procedure == ELSTER_PROCEDURE_VAT_ANNUAL
            else "MOCK-USTVA"
        )
        display_name = vat_return_display_name(vat_return.period_label)
        ticket = f"{prefix}-{vat_return.period_label}-{payload_hash[:12].upper()}"
        protocol = (
            f"ELSTER mock transport accepted {display_name} "
            f"{vat_return.period_label}; payload_sha256={payload_hash}; ticket={ticket}"
        )
        return ElsterTransportResult(
            status="transmitted",
            transfer_ticket=ticket,
            response_protocol=protocol,
        )


@dataclass(frozen=True, slots=True)
class EricElsterTransport:
    eric_library_path: str | None
    certificate_path: str | None
    certificate_alias: str | None = None
    eric_command: str | None = None
    timeout_seconds: int = 60
    certificate_pin: str | None = None
    log_dir: str | None = None
    # Zertifikat der Gesellschaft (entschlüsselt); hat Vorrang vor ``certificate_path``.
    certificate_content: bytes | None = None
    name: str = "eric"

    def transmit(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn
    ) -> ElsterTransportResult:
        if not self.eric_library_path or not Path(self.eric_library_path).exists():
            raise ElsterError("ERiC library is not configured.")
        if not self.certificate_content and (
            not self.certificate_path or not Path(self.certificate_path).exists()
        ):
            raise ElsterError("ELSTER certificate is not configured.")
        response = self.run(
            payload_xml=payload_xml,
            payload_hash=payload_hash,
            vat_return=vat_return,
            mode="send",
        )
        status = str(response.get("status") or "transmitted")
        transfer_ticket = str(response.get("transfer_ticket") or "").strip()
        if status not in {"transmitted", "failed"}:
            raise ElsterError("ERiC command returned invalid status.")
        if status == "transmitted" and not transfer_ticket:
            raise ElsterError("ERiC command returned no transfer ticket.")
        return ElsterTransportResult(
            status=status,
            transfer_ticket=transfer_ticket,
            response_protocol=str(response.get("response_protocol") or "ERiC command completed."),
        )

    def validate(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn
    ) -> dict[str, object]:
        """Prüft den Datensatz lokal mit ERiC, ohne zu senden."""
        return self.run(
            payload_xml=payload_xml,
            payload_hash=payload_hash,
            vat_return=vat_return,
            mode="validate",
        )

    def command(self) -> list[str]:
        if self.eric_command:
            return [self.eric_command]
        return [sys.executable, "-m", BUILTIN_ERIC_RUNNER]

    def run(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn, mode: str
    ) -> dict[str, object]:
        if not self.eric_library_path or not Path(self.eric_library_path).exists():
            raise ElsterError("ERiC library is not configured.")
        if self.eric_command and not Path(self.eric_command).exists():
            raise ElsterError("ERiC command is not configured.")

        with tempfile.TemporaryDirectory(prefix="openbuchhaltung-elster-") as tmp_dir:
            payload_path = Path(tmp_dir) / "payload.xml"
            response_path = Path(tmp_dir) / "response.json"
            payload_path.write_text(payload_xml, encoding="utf-8")
            certificate_path = self.certificate_path or ""
            if self.certificate_content and mode == "send":
                certificate_file = Path(tmp_dir) / "certificate.pfx"
                descriptor = os.open(certificate_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(self.certificate_content)
                certificate_path = str(certificate_file)

            env = os.environ.copy()
            env.update(
                {
                    "ELSTER_PAYLOAD_XML": str(payload_path),
                    "ELSTER_RESPONSE_JSON": str(response_path),
                    "ELSTER_PAYLOAD_SHA256": payload_hash,
                    "ELSTER_ERIC_MODE": mode,
                    "ELSTER_DATENART_VERSION": _datenart_version(vat_return),
                    "ELSTER_ERIC_LIBRARY_PATH": self.eric_library_path,
                    "ELSTER_ERIC_LOG_DIR": self.log_dir or tmp_dir,
                    "ELSTER_CERTIFICATE_PATH": certificate_path,
                    "ELSTER_CERTIFICATE_PIN": self.certificate_pin or "",
                    "ELSTER_CERTIFICATE_ALIAS": self.certificate_alias or "",
                    "ELSTER_VAT_RETURN_ID": str(vat_return.id),
                    "ELSTER_PERIOD_LABEL": vat_return.period_label,
                }
            )
            if not self.eric_command:
                env["PYTHONPATH"] = os.pathsep.join(
                    filter(None, [str(PROJECT_ROOT), env.get("PYTHONPATH")])
                )
            try:
                completed = subprocess.run(
                    self.command(),
                    cwd=tmp_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ElsterError("ERiC command timed out.") from exc

            if completed.returncode != 0:
                output = (completed.stderr or completed.stdout).strip()
                detail = f": {output}" if output else ""
                raise ElsterError(
                    f"ERiC command failed with exit code {completed.returncode}{detail}"
                )
            if not response_path.exists():
                raise ElsterError("ERiC command did not produce a response.")

            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ElsterError("ERiC command produced invalid JSON.") from exc
        if not isinstance(response, dict):
            raise ElsterError("ERiC command produced invalid JSON.")
        if not response.get("response_protocol") and completed.stdout:
            response["response_protocol"] = completed.stdout
        return response


def _datenart_version(vat_return: VatReturn) -> str:
    try:
        return ustva_datenart_version(ustva_period(vat_return.period_label).year)
    except ElsterXmlError:
        return ""


def elster_readiness(
    config: Mapping[str, object],
    company_certificate: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bereitschaft der Instanz; mit ``company_certificate`` (Status ohne Geheimnisse) zählt das
    Zertifikat der Gesellschaft vor dem Instanz-Zertifikat aus der Umgebung."""
    environment = str(config.get("ELSTER_ENVIRONMENT") or "test").strip().lower()
    hersteller_id = str(config.get("ELSTER_HERSTELLER_ID") or "").strip()
    eric_library_path = str(config.get("ELSTER_ERIC_LIBRARY_PATH") or "").strip()
    certificate_path = str(config.get("ELSTER_CERTIFICATE_PATH") or "").strip()
    certificate_pin_set = bool(str(config.get("ELSTER_CERTIFICATE_PIN") or ""))
    certificate_alias = str(config.get("ELSTER_CERTIFICATE_ALIAS") or "").strip()
    eric_command = str(config.get("ELSTER_ERIC_COMMAND") or "").strip()

    hersteller_id_ok = len(hersteller_id) == 5 and hersteller_id.isdigit()
    eric_library_exists = bool(eric_library_path and Path(eric_library_path).exists())
    certificate_exists = bool(certificate_path and Path(certificate_path).exists())
    certificate_detail = certificate_path or "not configured"
    certificate_source = "instance" if certificate_exists else None
    if company_certificate and company_certificate.get("configured"):
        # Das Zertifikat der Gesellschaft bringt seine PIN mit.
        expired = bool(company_certificate.get("expired"))
        certificate_exists = not expired
        certificate_pin_set = True
        certificate_alias = str(company_certificate.get("display_name") or "")
        valid_until = str(company_certificate.get("valid_until") or "")[:10]
        certificate_detail = (
            f"company: {certificate_alias} "
            f"({'expired' if expired else 'valid until'} {valid_until})"
        )
        certificate_source = "company"
    # Ohne ELSTER_ERIC_COMMAND läuft der integrierte Runner (python -m app.services.eric_runner).
    eric_command_exists = bool(eric_command and Path(eric_command).exists())
    eric_runner_ok = eric_command_exists if eric_command else True
    eric_validation_available = eric_library_exists and eric_runner_ok and hersteller_id_ok
    eric_configured = eric_validation_available and certificate_exists
    warnings: list[str] = []
    if environment not in ELSTER_ENVIRONMENTS:
        warnings.append("ELSTER_ENVIRONMENT must be 'test' or 'production'.")
    if not hersteller_id:
        warnings.append("ELSTER_HERSTELLER_ID is not configured.")
    elif not hersteller_id_ok:
        warnings.append("ELSTER_HERSTELLER_ID must have five digits.")
    if not eric_library_path:
        warnings.append("ELSTER_ERIC_LIBRARY_PATH is not configured.")
    elif not eric_library_exists:
        warnings.append("ELSTER_ERIC_LIBRARY_PATH does not exist.")
    if certificate_source == "company":
        if not certificate_exists:
            warnings.append("The company's ELSTER certificate has expired.")
    elif not certificate_path:
        warnings.append("ELSTER_CERTIFICATE_PATH is not configured.")
    elif not certificate_exists:
        warnings.append("ELSTER_CERTIFICATE_PATH does not exist.")
    if eric_command and not eric_command_exists:
        warnings.append("ELSTER_ERIC_COMMAND does not exist.")
    if environment == "production" and not eric_configured:
        warnings.append(
            "Production ELSTER requires Hersteller-ID, ERiC library and certificate."
        )
    if environment == "production" and not certificate_pin_set:
        warnings.append("ELSTER_CERTIFICATE_PIN is not configured.")
    missing_production_requirements = []
    if environment != "production":
        missing_production_requirements.append("ELSTER_ENVIRONMENT=production")
    if not hersteller_id_ok:
        missing_production_requirements.append("ELSTER_HERSTELLER_ID")
    if not eric_library_exists:
        missing_production_requirements.append("ELSTER_ERIC_LIBRARY_PATH")
    if not certificate_exists:
        missing_production_requirements.append("ELSTER_CERTIFICATE_PATH")
    if not certificate_pin_set:
        missing_production_requirements.append("ELSTER_CERTIFICATE_PIN")
    if not eric_runner_ok:
        missing_production_requirements.append("ELSTER_ERIC_COMMAND")
    checks = [
        {
            "id": "environment",
            "label": "ELSTER environment",
            "ok": environment in ELSTER_ENVIRONMENTS,
            "required_for_production": True,
            "detail": environment,
        },
        {
            "id": "mock_transport",
            "label": "Mock transport",
            "ok": True,
            "required_for_production": False,
            "detail": "available for test submissions",
        },
        {
            "id": "hersteller_id",
            "label": "Hersteller-ID",
            "ok": hersteller_id_ok,
            "required_for_production": True,
            "detail": hersteller_id or "not configured",
        },
        {
            "id": "eric_library",
            "label": "ERiC library",
            "ok": eric_library_exists,
            "required_for_production": True,
            "detail": eric_library_path or "not configured",
        },
        {
            "id": "certificate",
            "label": "ELSTER certificate",
            "ok": certificate_exists,
            "required_for_production": True,
            "detail": certificate_detail,
        },
        {
            "id": "certificate_pin",
            "label": "Certificate PIN",
            "ok": certificate_pin_set,
            "required_for_production": True,
            # Die PIN selbst wird nie ausgegeben.
            "detail": "configured" if certificate_pin_set else "not configured",
        },
        {
            "id": "eric_command",
            "label": "ERiC command runner",
            "ok": eric_runner_ok,
            "required_for_production": True,
            "detail": eric_command or f"built-in ({BUILTIN_ERIC_RUNNER})",
        },
        {
            "id": "certificate_alias",
            "label": "Certificate alias",
            "ok": bool(certificate_alias),
            "required_for_production": False,
            "detail": certificate_alias or "not configured",
        },
    ]

    return {
        "environment": environment,
        "mock_transport_available": True,
        "eric_transport_available": eric_configured,
        "eric_validation_available": eric_validation_available,
        "production_ready": environment == "production"
        and eric_configured
        and certificate_pin_set,
        "checks": checks,
        "missing_production_requirements": missing_production_requirements,
        "hersteller_id": hersteller_id or None,
        "eric_library_path": eric_library_path or None,
        "eric_library_exists": eric_library_exists,
        "certificate_path": certificate_path or None,
        "certificate_exists": certificate_exists,
        "certificate_source": certificate_source,
        "company_certificate": dict(company_certificate) if company_certificate else None,
        "certificate_pin_configured": certificate_pin_set,
        "certificate_alias": certificate_alias or None,
        "eric_command": eric_command or None,
        "eric_command_exists": eric_command_exists,
        "warnings": warnings,
    }


def vat_return_elster_procedure(vat_return: VatReturn) -> str:
    return (
        ELSTER_PROCEDURE_VAT_ANNUAL
        if vat_return_kind_from_label(vat_return.period_label) == VAT_RETURN_KIND_ANNUAL
        else ELSTER_PROCEDURE_USTVA
    )


def build_ustva_payload(vat_return: VatReturn) -> str:
    """Erzeugt eine interne, versionierte Umsatzsteuer-XML-Nutzlast.

    Diese XML ist noch kein amtliches ELSTER-ERiC-Transferformat. Sie ist der
    stabile Übergabepunkt für den späteren ERiC-Adapter.
    """
    procedure = vat_return_elster_procedure(vat_return)
    declaration_type = vat_return_kind_from_label(vat_return.period_label)

    root = ET.Element(
        ELSTER_PAYLOAD_ROOT,
        {
            "version": ELSTER_PAYLOAD_VERSION,
            "procedure": procedure,
        },
    )
    declaration = ET.SubElement(root, "VatReturn")
    ET.SubElement(declaration, "DeclarationType").text = declaration_type
    ET.SubElement(declaration, "TenantId").text = str(vat_return.tenant_id)
    ET.SubElement(declaration, "CompanyId").text = str(vat_return.company_id)
    ET.SubElement(declaration, "VatReturnId").text = str(vat_return.id)
    ET.SubElement(declaration, "Period").text = vat_return.period_label
    ET.SubElement(declaration, "DateFrom").text = vat_return.date_from.isoformat()
    ET.SubElement(declaration, "DateTo").text = vat_return.date_to.isoformat()

    values = ET.SubElement(declaration, "Kennzahlen")
    for row in vat_return.kennzahlen:
        item = ET.SubElement(values, "Kennzahl", {"code": str(row["kennziffer"])})
        ET.SubElement(item, "Label").text = str(row["label"])
        ET.SubElement(item, "Amount").text = str(row["amount"])

    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def elster_xml_options(*, environment: str, config: Mapping[str, object]) -> ElsterXmlOptions:
    return ElsterXmlOptions(
        hersteller_id=str(config.get("ELSTER_HERSTELLER_ID") or "").strip(),
        test=environment != "production",
        product_version=get_version(),
    )


def build_submission_payload(
    vat_return: VatReturn, *, environment: str, transport: str, config: Mapping[str, object]
) -> str:
    """Interne Nutzlast für den Mock, amtlicher ElsterXML-Datensatz für ERiC."""
    if transport != "eric":
        return build_ustva_payload(vat_return)
    if vat_return_elster_procedure(vat_return) != ELSTER_PROCEDURE_USTVA:
        raise ElsterXmlError(
            "Die USt-Jahreserklärung kann noch nicht über ERiC übermittelt werden."
        )
    return build_ustva_elster_xml(
        vat_return=vat_return,
        company=vat_return.company,
        options=elster_xml_options(environment=environment, config=config),
    )


def _company_certificate_status(session: Session, company_id: int) -> dict[str, object]:
    return serialize_company_elster_certificate(
        get_company_elster_certificate(session, company_id), company_id=company_id
    )


def _company_certificate_material(
    session: Session, company_id: int, config: Mapping[str, object]
) -> CertificateMaterial | None:
    """Entschlüsseltes Gesellschafts-Zertifikat; ``None`` ohne (dann gilt das der Instanz)."""
    certificate = get_company_elster_certificate(session, company_id)
    if certificate is None:
        return None
    status = serialize_company_elster_certificate(certificate)
    if status["expired"]:
        raise ElsterError("The company's ELSTER certificate has expired.")
    secret_key = str(config.get("SECRET_KEY") or "") or None
    try:
        return company_certificate_material(certificate, secret_key=secret_key)
    except ElsterCertificateError as exc:
        raise ElsterError(str(exc)) from exc


def submit_vat_return(
    *,
    session: Session,
    vat_return_id: int,
    environment: str,
    transport: str,
    changed_by: str,
    certificate_alias: str | None = None,
    config: Mapping[str, object] | None = None,
) -> ElsterSubmission:
    config = config or {}
    environment = environment.strip().lower()
    transport = transport.strip().lower()
    if environment not in ELSTER_ENVIRONMENTS:
        raise ElsterError("environment must be 'test' or 'production'.")
    if transport not in ELSTER_TRANSPORTS:
        raise ElsterError("transport must be 'mock' or 'eric'.")
    if transport == "mock" and environment != "test":
        raise ElsterError("Mock transport is only allowed for the test environment.")

    vat_return = session.get(VatReturn, vat_return_id)
    if vat_return is None:
        raise ElsterError("VAT return not found.")

    payload_error: str | None = None
    try:
        payload_xml = build_submission_payload(
            vat_return, environment=environment, transport=transport, config=config
        )
    except ElsterXmlError as exc:
        # Protokolliert wird der Snapshot, der nicht in einen Datensatz übersetzt werden konnte.
        payload_error = str(exc)
        payload_xml = build_ustva_payload(vat_return)
    payload_hash = hashlib.sha256(payload_xml.encode("utf-8")).hexdigest()
    certificate: CertificateMaterial | None = None
    if transport == "eric" and payload_error is None:
        try:
            certificate = _company_certificate_material(session, vat_return.company_id, config)
        except ElsterError as exc:
            payload_error = str(exc)
    effective_certificate_alias = (
        certificate_alias
        or (certificate.alias if certificate else None)
        or str(config.get("ELSTER_CERTIFICATE_ALIAS") or "")
        or None
    )
    transport_adapter = _transport_adapter(
        transport=transport,
        certificate_alias=effective_certificate_alias,
        config=config,
        certificate=certificate,
    )
    try:
        if payload_error is not None:
            raise ElsterError(payload_error)
        result = transport_adapter.transmit(
            payload_xml=payload_xml, payload_hash=payload_hash, vat_return=vat_return
        )
    except ElsterError as exc:
        submission = _record_submission(
            session=session,
            vat_return=vat_return,
            environment=environment,
            transport=transport,
            certificate_alias=effective_certificate_alias,
            payload_hash=payload_hash,
            payload_xml=payload_xml,
            status="failed",
            response_protocol=str(exc),
            transfer_ticket=None,
            changed_by=changed_by,
        )
        session.commit()
        session.refresh(submission)
        raise ElsterError(f"{exc} Submission {submission.id} was recorded as failed.") from exc

    submission = _record_submission(
        session=session,
        vat_return=vat_return,
        environment=environment,
        transport=transport,
        certificate_alias=effective_certificate_alias,
        payload_hash=payload_hash,
        payload_xml=payload_xml,
        status=result.status,
        response_protocol=result.response_protocol,
        transfer_ticket=result.transfer_ticket,
        changed_by=changed_by,
    )
    if result.status != "transmitted":
        session.commit()
        session.refresh(submission)
        return submission

    vat_return.status = "uebermittelt"
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=vat_return.tenant_id,
        company_id=vat_return.company_id,
        entity_type="vat_return",
        entity_id=str(vat_return.id),
        action="elster_transmitted",
        changed_by=changed_by,
        payload={
            "submission_id": submission.id,
            "environment": submission.environment,
            "transport": submission.transport,
            "transfer_ticket": submission.transfer_ticket,
        },
    )
    session.commit()
    session.refresh(submission)
    return submission


def preflight_vat_return(
    *,
    session: Session,
    vat_return_id: int,
    environment: str,
    transport: str,
    certificate_alias: str | None = None,
    config: Mapping[str, object] | None = None,
) -> dict[str, object]:
    environment = environment.strip().lower()
    transport = transport.strip().lower()
    errors: list[str] = []
    if environment not in ELSTER_ENVIRONMENTS:
        errors.append("environment must be 'test' or 'production'.")
    if transport not in ELSTER_TRANSPORTS:
        errors.append("transport must be 'mock' or 'eric'.")
    if transport == "mock" and environment != "test":
        errors.append("Mock transport is only allowed for the test environment.")

    vat_return = session.get(VatReturn, vat_return_id)
    if vat_return is None:
        errors.append("VAT return not found.")
        return {
            "ok": False,
            "errors": errors,
            "warnings": [],
            "vat_return_id": vat_return_id,
            "environment": environment,
            "transport": transport,
            "certificate_alias": certificate_alias,
            "payload_hash": None,
            "payload_size": 0,
            "period_label": None,
            "readiness": elster_readiness(config or {}),
        }

    payload_xml = build_ustva_payload(vat_return)
    payload_bytes = payload_xml.encode("utf-8")
    payload = _payload_diagnostics(payload_xml)
    readiness = elster_readiness(
        config or {}, _company_certificate_status(session, vat_return.company_id)
    )
    warnings = list(readiness["warnings"])
    max_payload_bytes = int((config or {}).get("ELSTER_MAX_PAYLOAD_BYTES") or 0)
    if transport == "eric" and not readiness["eric_transport_available"]:
        errors.append("ERiC transport is not configured.")
    if environment == "production" and not readiness["production_ready"]:
        errors.append("Production ELSTER is not ready.")
    if environment != readiness["environment"]:
        warnings.append("Requested ELSTER environment differs from configured environment.")
    if vat_return.status == "uebermittelt":
        display_name = vat_return_display_name(vat_return.period_label)
        warnings.append(f"{display_name} was already transmitted.")
    if payload["root_tag"] != ELSTER_PAYLOAD_ROOT:
        errors.append("ELSTER payload root is invalid.")
    if payload["version"] != ELSTER_PAYLOAD_VERSION:
        errors.append("ELSTER payload version is invalid.")
    if payload["procedure"] != vat_return_elster_procedure(vat_return):
        errors.append("ELSTER payload procedure is invalid.")
    if payload["period_label"] != vat_return.period_label:
        errors.append("ELSTER payload period does not match the VAT return.")
    if payload["date_from"] != vat_return.date_from.isoformat():
        errors.append("ELSTER payload start date does not match the UStVA.")
    if payload["date_to"] != vat_return.date_to.isoformat():
        errors.append("ELSTER payload end date does not match the UStVA.")
    if payload["kennzahlen_count"] == 0:
        errors.append("ELSTER payload contains no Kennzahlen.")
    if "83" not in payload["kennzahl_codes"]:
        errors.append("ELSTER payload must contain Kennzahl 83.")
    if max_payload_bytes and len(payload_bytes) > max_payload_bytes:
        errors.append("ELSTER payload exceeds ELSTER_MAX_PAYLOAD_BYTES.")
    elster_xml_info: dict[str, object] | None = None
    eric_validation: dict[str, object] | None = None
    if transport == "eric":
        elster_xml_info, eric_validation = _preflight_elster_xml(
            vat_return=vat_return,
            environment=environment,
            certificate_alias=certificate_alias,
            readiness=readiness,
            config=config or {},
            errors=errors,
        )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "vat_return_id": vat_return.id,
        "environment": environment,
        "transport": transport,
        "certificate_alias": certificate_alias
        or readiness.get("certificate_alias"),
        "payload": payload,
        "payload_hash": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_size": len(payload_bytes),
        "period_label": vat_return.period_label,
        "declaration_type": vat_return_kind_from_label(vat_return.period_label),
        "readiness": readiness,
        "elster_xml": elster_xml_info,
        "eric_validation": eric_validation,
    }


def _preflight_elster_xml(
    *,
    vat_return: VatReturn,
    environment: str,
    certificate_alias: str | None,
    readiness: Mapping[str, object],
    config: Mapping[str, object],
    errors: list[str],
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Baut den ElsterXML-Datensatz und prüft ihn – falls ERiC eingebunden ist – lokal."""
    try:
        elster_xml = build_submission_payload(
            vat_return, environment=environment, transport="eric", config=config
        )
    except ElsterXmlError as exc:
        errors.append(str(exc))
        return {"ok": False, "error": str(exc)}, None
    elster_bytes = elster_xml.encode("utf-8")
    elster_hash = hashlib.sha256(elster_bytes).hexdigest()
    info: dict[str, object] = {
        "ok": True,
        "datenart_version": _datenart_version(vat_return),
        "testmerker": environment != "production",
        "payload_hash": elster_hash,
        "payload_size": len(elster_bytes),
    }
    if not readiness.get("eric_validation_available"):
        return info, None
    adapter = _transport_adapter(
        transport="eric", certificate_alias=certificate_alias, config=config
    )
    try:
        response = adapter.validate(
            payload_xml=elster_xml, payload_hash=elster_hash, vat_return=vat_return
        )
    except ElsterError as exc:
        errors.append(f"ERiC-Prüfung nicht möglich: {exc}")
        return info, {"status": "error", "response_protocol": str(exc), "findings": []}
    findings = [str(item) for item in response.get("findings") or []]
    validation = {
        "status": str(response.get("status") or ""),
        "eric_code": response.get("eric_code"),
        "response_protocol": str(response.get("response_protocol") or ""),
        "findings": findings,
    }
    if validation["status"] != "validated":
        detail = "; ".join(findings) or validation["response_protocol"]
        errors.append(f"ERiC-Prüfung fehlgeschlagen: {detail}")
    return info, validation


def retry_elster_submission(
    *,
    session: Session,
    submission_id: int,
    changed_by: str,
    config: Mapping[str, object] | None = None,
) -> ElsterSubmission:
    submission = session.get(ElsterSubmission, submission_id)
    if submission is None:
        raise ElsterError("ELSTER submission not found.")
    if submission.status != "failed":
        raise ElsterError("Only failed ELSTER submissions can be retried.")
    return submit_vat_return(
        session=session,
        vat_return_id=submission.vat_return_id,
        environment=submission.environment,
        transport=submission.transport,
        certificate_alias=submission.certificate_alias,
        changed_by=changed_by,
        config=config,
    )


def _record_submission(
    *,
    session: Session,
    vat_return: VatReturn,
    environment: str,
    transport: str,
    certificate_alias: str | None,
    payload_hash: str,
    payload_xml: str,
    status: str,
    response_protocol: str | None,
    transfer_ticket: str | None,
    changed_by: str,
) -> ElsterSubmission:
    submission = ElsterSubmission(
        tenant_id=vat_return.tenant_id,
        company_id=vat_return.company_id,
        vat_return_id=vat_return.id,
        procedure=vat_return_elster_procedure(vat_return),
        environment=environment,
        status=status,
        transport=transport,
        certificate_alias=certificate_alias,
        payload_hash=payload_hash,
        payload_xml=payload_xml,
        response_protocol=response_protocol,
        transfer_ticket=transfer_ticket,
        submitted_at=datetime.now(timezone.utc) if status == "transmitted" else None,
        created_by=changed_by,
    )
    session.add(submission)
    session.flush()

    log_audit_event(
        session=session,
        tenant_id=submission.tenant_id,
        company_id=submission.company_id,
        entity_type="elster_submission",
        entity_id=str(submission.id),
        action=status,
        changed_by=changed_by,
        payload={
            "procedure": submission.procedure,
            "environment": submission.environment,
            "transport": submission.transport,
            "status": submission.status,
            "vat_return_id": vat_return.id,
            "period_label": vat_return.period_label,
            "payload_hash": submission.payload_hash,
            "transfer_ticket": submission.transfer_ticket,
        },
    )
    if status in {"transmitted", "failed"}:
        notify_elster_submission(
            session,
            company_id=submission.company_id,
            document=vat_return_display_name(vat_return.period_label),
            period_label=vat_return.period_label,
            status=status,
        )
    return submission


def list_elster_submissions(
    *,
    session: Session,
    company_id: int,
    vat_return_id: int | None = None,
    status: str | None = None,
    transport: str | None = None,
    environment: str | None = None,
) -> list[ElsterSubmission]:
    status = status.strip().lower() if status else None
    transport = transport.strip().lower() if transport else None
    environment = environment.strip().lower() if environment else None
    if status is not None and status not in ELSTER_STATUSES:
        raise ElsterError("status must be 'created', 'transmitted' or 'failed'.")
    if transport is not None and transport not in ELSTER_TRANSPORTS:
        raise ElsterError("transport must be 'mock' or 'eric'.")
    if environment is not None and environment not in ELSTER_ENVIRONMENTS:
        raise ElsterError("environment must be 'test' or 'production'.")

    stmt = (
        select(ElsterSubmission)
        .where(ElsterSubmission.company_id == company_id)
        .order_by(ElsterSubmission.created_at.desc(), ElsterSubmission.id.desc())
    )
    if vat_return_id is not None:
        stmt = stmt.where(ElsterSubmission.vat_return_id == vat_return_id)
    if status is not None:
        stmt = stmt.where(ElsterSubmission.status == status)
    if transport is not None:
        stmt = stmt.where(ElsterSubmission.transport == transport)
    if environment is not None:
        stmt = stmt.where(ElsterSubmission.environment == environment)
    return session.execute(stmt).scalars().all()


def elster_submission_summary(*, session: Session, company_id: int) -> dict[str, object]:
    submissions = list_elster_submissions(session=session, company_id=company_id)
    by_status = {status: 0 for status in sorted(ELSTER_STATUSES)}
    by_transport = {transport: 0 for transport in sorted(ELSTER_TRANSPORTS)}
    by_environment = {environment: 0 for environment in sorted(ELSTER_ENVIRONMENTS)}
    for submission in submissions:
        by_status[submission.status] = by_status.get(submission.status, 0) + 1
        by_transport[submission.transport] = by_transport.get(submission.transport, 0) + 1
        by_environment[submission.environment] = (
            by_environment.get(submission.environment, 0) + 1
        )

    latest = submissions[0] if submissions else None
    return {
        "company_id": company_id,
        "total": len(submissions),
        "by_status": by_status,
        "by_transport": by_transport,
        "by_environment": by_environment,
        "latest": {
            "id": latest.id,
            "status": latest.status,
            "transport": latest.transport,
            "environment": latest.environment,
            "created_at": latest.created_at.isoformat(),
        }
        if latest is not None
        else None,
    }


def get_elster_submission(
    *, session: Session, submission_id: int
) -> ElsterSubmission | None:
    return session.get(ElsterSubmission, submission_id)


def elster_payload_filename(submission: ElsterSubmission) -> str:
    period_label = submission.vat_return.period_label.replace("/", "-")
    return f"elster-{submission.procedure}-{period_label}-{submission.id}.xml"


def elster_payload_hash_matches(submission: ElsterSubmission) -> bool:
    payload_hash = hashlib.sha256(submission.payload_xml.encode("utf-8")).hexdigest()
    return payload_hash == submission.payload_hash


def _payload_diagnostics(payload_xml: str) -> dict[str, object]:
    root = ET.fromstring(payload_xml)
    declaration = root.find("VatReturn")
    kennzahlen = declaration.find("Kennzahlen") if declaration is not None else None
    kennzahl_items = list(kennzahlen) if kennzahlen is not None else []
    return {
        "root_tag": root.tag,
        "version": root.attrib.get("version"),
        "procedure": root.attrib.get("procedure"),
        "declaration_type": declaration.findtext("DeclarationType")
        if declaration is not None
        else None,
        "period_label": declaration.findtext("Period") if declaration is not None else None,
        "date_from": declaration.findtext("DateFrom") if declaration is not None else None,
        "date_to": declaration.findtext("DateTo") if declaration is not None else None,
        "kennzahlen_count": len(kennzahl_items),
        "kennzahl_codes": [str(item.attrib.get("code")) for item in kennzahl_items],
    }


def _transport_adapter(
    *,
    transport: str,
    certificate_alias: str | None,
    config: Mapping[str, object],
    certificate: CertificateMaterial | None = None,
) -> ElsterTransport:
    if transport == "mock":
        return MockElsterTransport()
    return EricElsterTransport(
        eric_library_path=str(config.get("ELSTER_ERIC_LIBRARY_PATH") or "") or None,
        certificate_path=str(config.get("ELSTER_CERTIFICATE_PATH") or "") or None,
        certificate_pin=certificate.pin
        if certificate
        else str(config.get("ELSTER_CERTIFICATE_PIN") or "") or None,
        certificate_content=certificate.content if certificate else None,
        log_dir=str(config.get("ELSTER_ERIC_LOG_DIR") or "") or None,
        certificate_alias=certificate_alias
        or str(config.get("ELSTER_CERTIFICATE_ALIAS") or "")
        or None,
        eric_command=str(config.get("ELSTER_ERIC_COMMAND") or "") or None,
        timeout_seconds=int(config.get("ELSTER_ERIC_TIMEOUT_SECONDS") or 60),
    )
