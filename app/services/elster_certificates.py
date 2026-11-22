"""ELSTER-Zertifikat und PIN je Gesellschaft (ADR-011).

Beim Hochladen wird die Datei (Soft-PSE, PKCS#12/.pfx) mit der PIN geöffnet: Eine falsche PIN,
eine kaputte oder abgelaufene Datei wird sofort abgewiesen, nicht erst beim Senden. Datei und PIN
werden Fernet-verschlüsselt gespeichert (Schlüssel aus ``SECRET_KEY``) und nur für den ERiC-Aufruf
entschlüsselt; ausgegeben werden sie nie. Das Audit-Log hält Inhaber, Gültigkeit und
Fingerabdruck fest, keine Geheimnisse.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, pkcs12
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import Company, CompanyElsterCertificate

MAX_CERTIFICATE_BYTES = 64 * 1024
_HKDF_INFO = b"openbuchhaltung-elster-certificate"
# Schlüssel der Soft-PSE: Signatur und Verschlüsselung; CA-Zertifikate zählen nicht zur Gültigkeit.
_KEY_FRIENDLY_NAMES = {b"signaturekey", b"encryptionkey"}


class ElsterCertificateError(ValueError):
    """Das Zertifikat kann nicht gespeichert oder gelesen werden."""


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    subject: str
    issuer: str | None
    valid_from: datetime
    valid_until: datetime
    fingerprint_sha256: str


@dataclass(frozen=True, slots=True)
class CertificateMaterial:
    """Entschlüsselte Datei und PIN für genau einen ERiC-Aufruf."""

    content: bytes
    pin: str
    alias: str


def _fernet(secret_key: str | None) -> Fernet:
    if not secret_key:
        raise ElsterCertificateError(
            "SECRET_KEY fehlt – das Zertifikat kann nicht verschlüsselt werden."
        )
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        str(secret_key).encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def inspect_certificate(
    content: bytes, pin: str, *, now: datetime | None = None
) -> CertificateInfo:
    """Öffnet die PKCS#12-Datei mit der PIN und liest Inhaber und Gültigkeit."""
    if not content:
        raise ElsterCertificateError("Die Zertifikatsdatei ist leer.")
    if len(content) > MAX_CERTIFICATE_BYTES:
        raise ElsterCertificateError("Die Zertifikatsdatei ist zu groß (höchstens 64 KB).")
    if not pin:
        raise ElsterCertificateError("Die PIN des Zertifikats fehlt.")
    try:
        bundle = pkcs12.load_pkcs12(content, pin.encode("utf-8"))
    except ValueError as exc:
        raise ElsterCertificateError(
            "Die Datei ist kein lesbares ELSTER-Zertifikat (.pfx) oder die PIN ist falsch."
        ) from exc
    if bundle.key is None or bundle.cert is None:
        raise ElsterCertificateError("Die Datei enthält keinen privaten Schlüssel mit Zertifikat.")
    certificates = [bundle.cert.certificate] + [
        item.certificate
        for item in bundle.additional_certs
        if item.friendly_name in _KEY_FRIENDLY_NAMES
    ]
    valid_from = max(cert.not_valid_before_utc for cert in certificates)
    valid_until = min(cert.not_valid_after_utc for cert in certificates)
    now = now or datetime.now(timezone.utc)
    if valid_until <= now:
        raise ElsterCertificateError(
            f"Das Zertifikat ist am {valid_until:%d.%m.%Y} abgelaufen."
        )
    main = bundle.cert.certificate
    return CertificateInfo(
        subject=main.subject.rfc4514_string()[:255],
        issuer=main.issuer.rfc4514_string()[:255] or None,
        valid_from=valid_from,
        valid_until=valid_until,
        fingerprint_sha256=hashlib.sha256(main.public_bytes(Encoding.DER)).hexdigest(),
    )


def get_company_elster_certificate(
    session: Session, company_id: int
) -> CompanyElsterCertificate | None:
    return session.scalar(
        select(CompanyElsterCertificate).where(CompanyElsterCertificate.company_id == company_id)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def certificate_display_name(certificate: CompanyElsterCertificate) -> str:
    return certificate.alias or certificate.subject


def serialize_company_elster_certificate(
    certificate: CompanyElsterCertificate | None, *, company_id: int | None = None
) -> dict[str, Any]:
    """Status ohne Geheimnisse."""
    if certificate is None:
        return {"company_id": company_id, "configured": False}
    valid_until = _aware(certificate.valid_until)
    return {
        "company_id": certificate.company_id,
        "configured": True,
        "alias": certificate.alias,
        "display_name": certificate_display_name(certificate),
        "file_name": certificate.file_name,
        "subject": certificate.subject,
        "issuer": certificate.issuer,
        "valid_from": _aware(certificate.valid_from).isoformat(),
        "valid_until": valid_until.isoformat(),
        "expired": valid_until <= datetime.now(timezone.utc),
        "fingerprint_sha256": certificate.fingerprint_sha256,
        "uploaded_at": _aware(certificate.uploaded_at).isoformat(),
        "uploaded_by": certificate.uploaded_by,
    }


def _audit_payload(certificate: CompanyElsterCertificate) -> dict[str, Any]:
    return {
        "alias": certificate.alias,
        "file_name": certificate.file_name,
        "subject": certificate.subject,
        "valid_until": _aware(certificate.valid_until).isoformat(),
        "fingerprint_sha256": certificate.fingerprint_sha256,
    }


def set_company_elster_certificate(
    *,
    session: Session,
    company: Company,
    content: bytes,
    pin: str,
    changed_by: str,
    secret_key: str | None,
    alias: str | None = None,
    file_name: str | None = None,
) -> CompanyElsterCertificate:
    """Prüft und speichert das Zertifikat; ein vorhandenes wird ersetzt. Ohne Commit."""
    info = inspect_certificate(content, pin)
    fernet = _fernet(secret_key)
    alias = " ".join((alias or "").split())[:120] or None
    file_name = " ".join((file_name or "").split())[:255] or None

    certificate = get_company_elster_certificate(session, company.id)
    replaced = certificate is not None
    if certificate is None:
        certificate = CompanyElsterCertificate(tenant_id=company.tenant_id, company_id=company.id)
        session.add(certificate)
    certificate.certificate_encrypted = fernet.encrypt(content).decode("ascii")
    certificate.pin_encrypted = fernet.encrypt(pin.encode("utf-8")).decode("ascii")
    certificate.alias = alias
    certificate.file_name = file_name
    certificate.subject = info.subject
    certificate.issuer = info.issuer
    certificate.valid_from = info.valid_from
    certificate.valid_until = info.valid_until
    certificate.fingerprint_sha256 = info.fingerprint_sha256
    certificate.uploaded_at = datetime.now(timezone.utc)
    certificate.uploaded_by = changed_by
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company_elster_certificate",
        entity_id=str(certificate.id),
        action="replaced" if replaced else "created",
        changed_by=changed_by,
        payload=_audit_payload(certificate),
    )
    return certificate


def clear_company_elster_certificate(
    *, session: Session, company: Company, changed_by: str
) -> bool:
    """Löscht Zertifikat und PIN der Gesellschaft; ``False``, wenn keines hinterlegt war."""
    certificate = get_company_elster_certificate(session, company.id)
    if certificate is None:
        return False
    payload = _audit_payload(certificate)
    certificate_id = certificate.id
    session.delete(certificate)
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company_elster_certificate",
        entity_id=str(certificate_id),
        action="deleted",
        changed_by=changed_by,
        payload=payload,
    )
    return True


def company_certificate_material(
    certificate: CompanyElsterCertificate, *, secret_key: str | None
) -> CertificateMaterial:
    """Entschlüsselt Datei und PIN für einen ERiC-Aufruf."""
    fernet = _fernet(secret_key)
    try:
        content = fernet.decrypt(certificate.certificate_encrypted.encode("ascii"))
        pin = fernet.decrypt(certificate.pin_encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise ElsterCertificateError(
            "Das hinterlegte ELSTER-Zertifikat ist nicht lesbar (SECRET_KEY geändert?). "
            "Bitte neu hochladen."
        ) from exc
    return CertificateMaterial(
        content=content, pin=pin, alias=certificate_display_name(certificate)
    )
