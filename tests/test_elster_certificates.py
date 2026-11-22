"""ELSTER-Zertifikat und PIN je Gesellschaft (ADR-011)."""

from __future__ import annotations

import base64
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.services.elster import (
    ElsterError,
    elster_readiness,
    preflight_vat_return,
    submit_vat_return,
)
from app.services.elster_certificates import (
    ElsterCertificateError,
    clear_company_elster_certificate,
    company_certificate_material,
    get_company_elster_certificate,
    inspect_certificate,
    serialize_company_elster_certificate,
    set_company_elster_certificate,
)
from app.services.vat_returns import save_vat_return
from domain.models import AuditLog, Base, CompanyElsterCertificate, User
from tests.test_vat_returns import _create_test_app, _make_eric_config, _seed, _seed_bookings

SECRET_KEY = "test-secret-key"
PIN = "123456"


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
        del connection_record
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _pfx(*, pin: str = PIN, days: int = 365, subject: str = "Muster GmbH") -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=400))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"signaturekey",
        key,
        certificate,
        None,
        serialization.BestAvailableEncryption(pin.encode("utf-8")),
    )


def _set(session: Session, company, content: bytes | None = None, **kwargs):
    return set_company_elster_certificate(
        session=session,
        company=company,
        content=content if content is not None else _pfx(),
        pin=kwargs.pop("pin", PIN),
        changed_by="pytest",
        secret_key=kwargs.pop("secret_key", SECRET_KEY),
        **kwargs,
    )


def test_inspect_certificate_reads_subject_and_validity() -> None:
    info = inspect_certificate(_pfx(), PIN)
    assert info.subject == "CN=Muster GmbH"
    assert info.valid_until > datetime.now(timezone.utc)
    assert len(info.fingerprint_sha256) == 64


@pytest.mark.parametrize(
    ("content", "pin", "message"),
    [
        (b"", PIN, "leer"),
        (b"x" * (64 * 1024 + 1), PIN, "zu groß"),
        (b"kein zertifikat", PIN, "PIN ist falsch"),
        (None, "000000", "PIN ist falsch"),
        (None, "", "PIN des Zertifikats fehlt"),
    ],
)
def test_inspect_certificate_rejects_bad_input(content, pin, message) -> None:
    with pytest.raises(ElsterCertificateError, match=message):
        inspect_certificate(_pfx() if content is None else content, pin)


def test_inspect_certificate_rejects_expired() -> None:
    with pytest.raises(ElsterCertificateError, match="abgelaufen"):
        inspect_certificate(_pfx(days=-1), PIN)


def test_set_replace_and_clear_company_certificate(session: Session) -> None:
    company = _seed(session)
    certificate = _set(session, company, alias=" Hauptzertifikat ", file_name="muster.pfx")
    session.commit()

    assert certificate.alias == "Hauptzertifikat"
    # Datei und PIN liegen nur verschlüsselt vor.
    assert PIN not in certificate.pin_encrypted
    status = serialize_company_elster_certificate(certificate)
    assert status["configured"] is True
    assert status["display_name"] == "Hauptzertifikat"
    assert status["subject"] == "CN=Muster GmbH"
    assert status["expired"] is False
    assert "pin" not in " ".join(status).lower()
    assert "certificate_encrypted" not in status

    material = company_certificate_material(certificate, secret_key=SECRET_KEY)
    assert material.pin == PIN
    assert material.alias == "Hauptzertifikat"
    inspect_certificate(material.content, material.pin)
    with pytest.raises(ElsterCertificateError, match="nicht lesbar"):
        company_certificate_material(certificate, secret_key="anderer-schluessel")

    replaced = _set(session, company, _pfx(subject="Neu GmbH"))
    session.commit()
    assert replaced.id == certificate.id
    assert session.scalar(select(CompanyElsterCertificate.subject)) == "CN=Neu GmbH"

    assert clear_company_elster_certificate(session=session, company=company, changed_by="pytest")
    session.commit()
    assert get_company_elster_certificate(session, company.id) is None
    assert not clear_company_elster_certificate(
        session=session, company=company, changed_by="pytest"
    )
    actions = [
        audit.action
        for audit in session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "company_elster_certificate")
        )
    ]
    assert actions == ["created", "replaced", "deleted"]
    for audit in session.scalars(
        select(AuditLog).where(AuditLog.entity_type == "company_elster_certificate")
    ):
        assert PIN not in str(audit.payload)


def test_set_company_certificate_requires_secret_key(session: Session) -> None:
    company = _seed(session)
    with pytest.raises(ElsterCertificateError, match="SECRET_KEY"):
        _set(session, company, secret_key=None)


def test_readiness_prefers_company_certificate(session: Session, tmp_path: Path) -> None:
    company = _seed(session)
    config = _make_eric_config(tmp_path)
    del config["ELSTER_CERTIFICATE_PATH"], config["ELSTER_CERTIFICATE_PIN"]
    instance = elster_readiness(config)
    assert instance["production_ready"] is False
    assert instance["certificate_source"] is None

    status = serialize_company_elster_certificate(
        _set(session, company, alias="Firma"), company_id=company.id
    )
    readiness = elster_readiness(config, status)
    assert readiness["production_ready"] is True
    assert readiness["certificate_source"] == "company"
    assert readiness["certificate_alias"] == "Firma"
    check = next(item for item in readiness["checks"] if item["id"] == "certificate")
    assert check["ok"] is True
    assert check["detail"].startswith("company: Firma (valid until ")

    expired = elster_readiness(config, {**status, "expired": True})
    assert expired["production_ready"] is False
    assert "The company's ELSTER certificate has expired." in expired["warnings"]


def _write_certificate_checking_runner(tmp_path: Path, expected: bytes) -> Path:
    """Fake-Runner: meldet, ob Datei und PIN der Gesellschaft ankommen, ohne sie auszugeben."""
    (tmp_path / "expected.pfx").write_bytes(expected)
    command = tmp_path / "cert-runner.py"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, stat\n"
        "mode = os.environ['ELSTER_ERIC_MODE']\n"
        "path = os.environ['ELSTER_CERTIFICATE_PATH']\n"
        "same = private = pin_ok = None\n"
        "if mode == 'send':\n"
        f"    expected = open({str(tmp_path / 'expected.pfx')!r}, 'rb').read()\n"
        "    same = open(path, 'rb').read() == expected\n"
        "    private = stat.S_IMODE(os.stat(path).st_mode) == 0o600\n"
        f"    pin_ok = os.environ['ELSTER_CERTIFICATE_PIN'] == {PIN!r}\n"
        "response = {\n"
        "    'status': 'transmitted' if mode == 'send' else 'validated',\n"
        "    'transfer_ticket': 'TT-1',\n"
        "    'response_protocol': f'same={same} private={private} pin_ok={pin_ok} path={path}',\n"
        "}\n"
        "json.dump(response, open(os.environ['ELSTER_RESPONSE_JSON'], 'w'))\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    return command


def test_submission_uses_company_certificate(session: Session, tmp_path: Path) -> None:
    company = _seed(session)
    _seed_bookings(session, company)
    content = _pfx()
    _set(session, company, content, alias="Firmenzertifikat")
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )
    config = {
        **_make_eric_config(tmp_path),
        "ELSTER_ERIC_COMMAND": str(_write_certificate_checking_runner(tmp_path, content)),
        "SECRET_KEY": SECRET_KEY,
    }
    del config["ELSTER_CERTIFICATE_PATH"], config["ELSTER_CERTIFICATE_PIN"]
    del config["ELSTER_CERTIFICATE_ALIAS"]

    preflight = preflight_vat_return(
        session=session,
        vat_return_id=vat_return.id,
        environment="production",
        transport="eric",
        config=config,
    )
    assert preflight["ok"] is True, preflight["errors"]
    assert preflight["readiness"]["certificate_source"] == "company"

    submission = submit_vat_return(
        session=session,
        vat_return_id=vat_return.id,
        environment="production",
        transport="eric",
        changed_by="pytest",
        config=config,
    )
    assert submission.status == "transmitted"
    assert submission.certificate_alias == "Firmenzertifikat"
    protocol = submission.response_protocol
    assert "same=True private=True pin_ok=True" in protocol
    # Die entschlüsselte Datei lag nur im temporären Verzeichnis des Aufrufs.
    assert not Path(protocol.split("path=", 1)[1]).exists()


def test_submission_fails_with_unreadable_company_certificate(
    session: Session, tmp_path: Path
) -> None:
    company = _seed(session)
    _seed_bookings(session, company)
    _set(session, company)
    vat_return = save_vat_return(
        session=session, company_id=company.id, period_label="2026-05", changed_by="pytest"
    )
    config = {**_make_eric_config(tmp_path), "SECRET_KEY": "geaenderter-schluessel"}
    with pytest.raises(ElsterError, match="nicht lesbar"):
        submit_vat_return(
            session=session,
            vat_return_id=vat_return.id,
            environment="production",
            transport="eric",
            changed_by="pytest",
            config=config,
        )


def test_certificate_api_and_web(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    app.config["SECRET_KEY"] = SECRET_KEY
    with app.extensions["db_session_factory"]() as db:
        company = _seed(db)
        db.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db.commit()
        company_id = company.id

    client = app.test_client()
    url = f"/api/v1/companies/{company_id}/elster-certificate"
    assert client.get(url).get_json() == {"company_id": company_id, "configured": False}

    wrong = client.post(
        url,
        json={"content_base64": base64.b64encode(_pfx()).decode(), "pin": "000000"},
    )
    assert wrong.status_code == 422
    assert "PIN" in wrong.get_json()["error"]
    assert client.post(url, json={"pin": PIN}).status_code == 400

    created = client.post(
        url,
        json={
            "content_base64": base64.b64encode(_pfx()).decode(),
            "pin": PIN,
            "alias": "API",
            "file_name": "api.pfx",
        },
    )
    assert created.status_code == 200
    body = created.get_json()
    assert body["display_name"] == "API"
    assert body["file_name"] == "api.pfx"
    assert PIN not in str(body)

    multipart = client.post(
        url,
        data={"file": (io.BytesIO(_pfx()), "multi.pfx"), "pin": PIN},
        content_type="multipart/form-data",
    )
    assert multipart.status_code == 200
    assert multipart.get_json()["file_name"] == "multi.pfx"

    readiness = client.get("/api/v1/elster/readiness", query_string={"company_id": company_id})
    assert readiness.get_json()["certificate_source"] == "company"
    assert client.get(
        "/api/v1/elster/readiness", query_string={"company_id": 9999}
    ).status_code == 404

    deleted = client.post(f"{url}/delete")
    assert deleted.get_json() == {"company_id": company_id, "configured": False, "deleted": True}

    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    upload = client.post(
        "/verwaltung/firmendaten/elster-zertifikat",
        data={
            "company_id": str(company_id),
            "file": (io.BytesIO(_pfx()), "web.pfx"),
            "pin": PIN,
            "alias": "Web",
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    html = upload.get_data(as_text=True)
    assert "ELSTER-Zertifikat gespeichert." in html
    assert "Zertifikat ersetzen" in html
    assert "CN=Muster GmbH" in html

    failed = client.post(
        "/verwaltung/firmendaten/elster-zertifikat",
        data={"company_id": str(company_id), "file": (io.BytesIO(b"x"), "x.pfx"), "pin": PIN},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "kein lesbares ELSTER-Zertifikat" in failed.get_data(as_text=True)

    cleared = client.post(
        "/verwaltung/firmendaten/elster-zertifikat/loeschen",
        data={"company_id": str(company_id)},
        follow_redirects=True,
    )
    assert "ELSTER-Zertifikat gelöscht." in cleared.get_data(as_text=True)
    assert "Kein Zertifikat hinterlegt." in cleared.get_data(as_text=True)
