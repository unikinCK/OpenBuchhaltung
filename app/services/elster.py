"""ELSTER-Grundlage: Payloads, Transportkante und Übermittlungsprotokolle.

Der erste Transport ist bewusst ein deterministischer Mock für die Testumgebung.
Die echte ERiC-Anbindung kann später hinter derselben Servicefunktion ergänzt
werden, ohne API/UI und Buchhaltungslogik umzubauen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import ElsterSubmission, VatReturn

ELSTER_ENVIRONMENTS = {"test", "production"}
ELSTER_STATUSES = {"created", "transmitted", "failed"}
ELSTER_TRANSPORTS = {"mock", "eric"}


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
        ticket = f"MOCK-USTVA-{vat_return.period_label}-{payload_hash[:12].upper()}"
        protocol = (
            "ELSTER mock transport accepted UStVA "
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
    name: str = "eric"

    def transmit(
        self, *, payload_xml: str, payload_hash: str, vat_return: VatReturn
    ) -> ElsterTransportResult:
        del payload_xml, payload_hash, vat_return
        if not self.eric_library_path or not Path(self.eric_library_path).exists():
            raise ElsterError("ERiC library is not configured.")
        if not self.certificate_path or not Path(self.certificate_path).exists():
            raise ElsterError("ELSTER certificate is not configured.")
        raise ElsterError("ERiC transport adapter is not implemented yet.")


def elster_readiness(config: Mapping[str, object]) -> dict[str, object]:
    environment = str(config.get("ELSTER_ENVIRONMENT") or "test").strip().lower()
    eric_library_path = str(config.get("ELSTER_ERIC_LIBRARY_PATH") or "").strip()
    certificate_path = str(config.get("ELSTER_CERTIFICATE_PATH") or "").strip()
    certificate_alias = str(config.get("ELSTER_CERTIFICATE_ALIAS") or "").strip()

    eric_library_exists = bool(eric_library_path and Path(eric_library_path).exists())
    certificate_exists = bool(certificate_path and Path(certificate_path).exists())
    eric_configured = eric_library_exists and certificate_exists
    warnings: list[str] = []
    if environment not in ELSTER_ENVIRONMENTS:
        warnings.append("ELSTER_ENVIRONMENT must be 'test' or 'production'.")
    if not eric_library_path:
        warnings.append("ELSTER_ERIC_LIBRARY_PATH is not configured.")
    elif not eric_library_exists:
        warnings.append("ELSTER_ERIC_LIBRARY_PATH does not exist.")
    if not certificate_path:
        warnings.append("ELSTER_CERTIFICATE_PATH is not configured.")
    elif not certificate_exists:
        warnings.append("ELSTER_CERTIFICATE_PATH does not exist.")
    if environment == "production" and not eric_configured:
        warnings.append("Production ELSTER requires ERiC library and certificate.")

    return {
        "environment": environment,
        "mock_transport_available": True,
        "eric_transport_available": eric_configured,
        "production_ready": environment == "production" and eric_configured,
        "eric_library_path": eric_library_path or None,
        "eric_library_exists": eric_library_exists,
        "certificate_path": certificate_path or None,
        "certificate_exists": certificate_exists,
        "certificate_alias": certificate_alias or None,
        "warnings": warnings,
    }


def build_ustva_payload(vat_return: VatReturn) -> str:
    """Erzeugt eine interne, versionierte UStVA-XML-Nutzlast.

    Diese XML ist noch kein amtliches ELSTER-ERiC-Transferformat. Sie ist der
    stabile Übergabepunkt für den späteren ERiC-Adapter.
    """

    root = ET.Element(
        "OpenBuchhaltungElster",
        {
            "version": "1",
            "procedure": "ustva",
        },
    )
    declaration = ET.SubElement(root, "VatReturn")
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
        raise ElsterError("UStVA not found.")

    payload_xml = build_ustva_payload(vat_return)
    payload_hash = hashlib.sha256(payload_xml.encode("utf-8")).hexdigest()
    transport_adapter = _transport_adapter(
        transport=transport, certificate_alias=certificate_alias, config=config or {}
    )
    try:
        result = transport_adapter.transmit(
            payload_xml=payload_xml, payload_hash=payload_hash, vat_return=vat_return
        )
    except ElsterError as exc:
        submission = _record_submission(
            session=session,
            vat_return=vat_return,
            environment=environment,
            transport=transport,
            certificate_alias=certificate_alias,
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
        certificate_alias=certificate_alias,
        payload_hash=payload_hash,
        payload_xml=payload_xml,
        status=result.status,
        response_protocol=result.response_protocol,
        transfer_ticket=result.transfer_ticket,
        changed_by=changed_by,
    )
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
        errors.append("UStVA not found.")
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
    readiness = elster_readiness(config or {})
    if transport == "eric" and not readiness["eric_transport_available"]:
        errors.append("ERiC transport is not configured.")
    if environment == "production" and not readiness["production_ready"]:
        errors.append("Production ELSTER is not ready.")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": readiness["warnings"],
        "vat_return_id": vat_return.id,
        "environment": environment,
        "transport": transport,
        "certificate_alias": certificate_alias
        or readiness.get("certificate_alias"),
        "payload_hash": hashlib.sha256(payload_xml.encode("utf-8")).hexdigest(),
        "payload_size": len(payload_xml.encode("utf-8")),
        "period_label": vat_return.period_label,
        "readiness": readiness,
    }


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
        procedure="ustva",
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


def _transport_adapter(
    *, transport: str, certificate_alias: str | None, config: Mapping[str, object]
) -> ElsterTransport:
    if transport == "mock":
        return MockElsterTransport()
    return EricElsterTransport(
        eric_library_path=str(config.get("ELSTER_ERIC_LIBRARY_PATH") or "") or None,
        certificate_path=str(config.get("ELSTER_CERTIFICATE_PATH") or "") or None,
        certificate_alias=certificate_alias
        or str(config.get("ELSTER_CERTIFICATE_ALIAS") or "")
        or None,
    )
