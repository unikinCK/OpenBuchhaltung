"""Zwei-Faktor-Anmeldung: TOTP nach RFC 6238 und Wiederherstellungscodes.

TOTP mit SHA-1, 6 Stellen und 30 Sekunden Schrittweite; toleriert wird je ein Schritt
Abweichung. Ein Zeitschritt gilt nur einmal (``user.totp_last_step``), damit ein
mitgelesener Code nicht erneut funktioniert. Das Verbrauchen läuft als bedingtes
``UPDATE``, damit auch parallele Anfragen auf mehreren Workern einen Code nur einmal
einlösen können.

Das Secret liegt mit Fernet verschlüsselt am Benutzer, der Schlüssel ist aus ``SECRET_KEY``
abgeleitet (eigener Ableitungszweck, getrennt von den LLM-API-Keys). Wird ``SECRET_KEY``
rotiert, ist es nicht mehr lesbar: TOTP-Codes schlagen dann fehl, Wiederherstellungscodes
gelten weiter, und ein Administrator setzt die Einrichtung per Verwaltung, REST, MCP oder
``flask reset-2fa`` zurück.

Wiederherstellungscodes sind 80-Bit-Zufallswerte. Wie bei den API-Tokens genügt deshalb
SHA-256 (je Benutzer gesalzen über die Benutzer-ID), ein langsamer Passwort-Hash würde jede
Prüfung nur verteuern.

Secrets und Codes dürfen weder ins Log noch ins Audit-Log noch in den Prüferexport. Alle
Funktionen committen nicht, das bleibt beim Aufrufer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import segno
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app, has_request_context, request
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash

from app.services.security_events import record_security_event
from domain.models import TwoFactorRecoveryCode, User

TOTP_DIGITS = 6
TOTP_PERIOD_SECONDS = 30
TOTP_WINDOW_STEPS = 1
TOTP_ISSUER = "OpenBuchhaltung"
TOTP_SECRET_BYTES = 20

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_LENGTH = 16
# 32 Zeichen ohne die leicht verwechselbaren I, O, 0 und 1: 16 Zeichen ergeben 80 Bit.
RECOVERY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

METHOD_TOTP = "totp"
METHOD_RECOVERY_CODE = "recovery_code"

_HKDF_INFO = b"openbuchhaltung:user-totp-secret:v1"

DEMO_MESSAGE = "In der Demo-Instanz lässt sich die Zwei-Faktor-Anmeldung nicht aktivieren."
PASSWORD_MESSAGE = "Das aktuelle Passwort ist falsch."
INVALID_CODE_MESSAGE = "Der Code ist ungültig oder wurde bereits verwendet."


class TwoFactorError(ValueError):
    """Die Aktion passt nicht zum Zustand der Zwei-Faktor-Anmeldung."""


class TwoFactorCredentialsError(TwoFactorError):
    """Passwort oder Code sind falsch."""


class TwoFactorBlockedError(TwoFactorError):
    """Zu viele Fehlversuche im Zeitfenster."""


# ---------------------------------------------------------------------------
# TOTP (RFC 4226 / RFC 6238)
# ---------------------------------------------------------------------------


def hotp(secret: bytes, counter: int, *, digits: int = TOTP_DIGITS) -> str:
    """Einmalcode nach RFC 4226 (HMAC-SHA-1 mit dynamischer Kürzung)."""
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp_step(timestamp: float) -> int:
    return int(timestamp) // TOTP_PERIOD_SECONDS


def decode_secret(secret: str) -> bytes:
    return base64.b32decode(secret, casefold=True)


def totp_code(secret: str, timestamp: float, *, digits: int = TOTP_DIGITS) -> str:
    return hotp(decode_secret(secret), totp_step(timestamp), digits=digits)


def matching_step(secret: str, code: str, *, at: float | None = None) -> int | None:
    """Zeitschritt im erlaubten Fenster, zu dem ``code`` passt, sonst ``None``.

    Geprüft wird das ganze Fenster ohne vorzeitigen Abbruch, damit die Laufzeit nichts
    über den Treffer verrät.
    """
    current = totp_step(time.time() if at is None else at)
    raw_secret = decode_secret(secret)
    matched: int | None = None
    for step in range(current - TOTP_WINDOW_STEPS, current + TOTP_WINDOW_STEPS + 1):
        if hmac.compare_digest(hotp(raw_secret, step).encode("ascii"), code.encode("utf-8")):
            matched = step
    return matched


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(TOTP_SECRET_BYTES)).decode("ascii")


def group_secret(secret: str) -> str:
    """Schlüssel in Vierergruppen zum Abtippen; Authenticator-Apps ignorieren die Leerzeichen."""
    return " ".join(secret[index : index + 4] for index in range(0, len(secret), 4))


# ---------------------------------------------------------------------------
# Verschlüsselung am Ruhepunkt
# ---------------------------------------------------------------------------


def _fernet(secret_key: str | None = None) -> Fernet:
    secret = secret_key if secret_key is not None else current_app.config.get("SECRET_KEY")
    if not secret:
        raise TwoFactorError("SECRET_KEY fehlt – das Secret kann nicht verschlüsselt werden.")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        str(secret).encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(secret: str, *, secret_key: str | None = None) -> str:
    return _fernet(secret_key).encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(token: str | None, *, secret_key: str | None = None) -> str | None:
    """Entschlüsselt ein gespeichertes Secret; ``None``, wenn es nicht lesbar ist."""
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        return None


# ---------------------------------------------------------------------------
# Provisioning: otpauth-URI und QR-Code
# ---------------------------------------------------------------------------


def _request_host() -> str | None:
    if not has_request_context():
        return None
    return urlsplit(request.url_root).hostname


def provisioning_uri(secret: str, *, username: str, host: str | None = None) -> str:
    """``otpauth://``-URI für Authenticator-Apps.

    Der Hostname steht im Kontonamen: Manche Apps überschreiben sonst den Eintrag, wenn
    derselbe Benutzername auf mehreren Instanzen eingerichtet wird.
    """
    account = f"{username}@{host}" if host else username
    label = f"{quote(TOTP_ISSUER)}:{quote(account, safe='@')}"
    query = urlencode(
        {
            "secret": secret,
            "issuer": TOTP_ISSUER,
            "algorithm": "SHA1",
            "digits": TOTP_DIGITS,
            "period": TOTP_PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def provisioning_uri_for(user: User, secret: str) -> str:
    return provisioning_uri(secret, username=user.username, host=_request_host())


def qr_code_svg(uri: str) -> str:
    """QR-Code als Inline-SVG, serverseitig gerendert (verträgt die strikte CSP)."""
    return segno.make(uri, error="m").svg_inline(omitsize=True, border=4, dark="#000", light="#fff")


# ---------------------------------------------------------------------------
# Rollenpflicht und Status
# ---------------------------------------------------------------------------


def required_roles() -> frozenset[str]:
    """Rollen aus ``TWO_FACTOR_REQUIRED_ROLES`` (kommagetrennt, ohne Groß-/Kleinschreibung)."""
    raw = current_app.config.get("TWO_FACTOR_REQUIRED_ROLES") or ""
    return frozenset(part.strip().casefold() for part in str(raw).split(",") if part.strip())


def two_factor_required_for(role: str) -> bool:
    if current_app.config.get("DEMO_MODE"):
        return False
    return role.casefold() in required_roles()


def two_factor_enabled(user: User) -> bool:
    return user.totp_enabled_at is not None


def two_factor_summary(user: User) -> dict[str, Any]:
    """Status für Benutzerlisten (ohne Datenbankabfrage)."""
    enabled = two_factor_enabled(user)
    return {
        "enabled": enabled,
        "enabled_at": user.totp_enabled_at.isoformat() if enabled else None,
        "setup_pending": not enabled and user.totp_secret_encrypted is not None,
        "required": two_factor_required_for(user.role),
    }


def two_factor_status(session: Session, user: User) -> dict[str, Any]:
    """Status für einen Benutzer; enthält nie Secret oder Codes."""
    status = two_factor_summary(user)
    status["user_id"] = user.id
    status["username"] = user.username
    status["recovery_codes_remaining"] = _remaining_recovery_codes(session, user)
    # Nach einer Rotation von SECRET_KEY lässt sich das Secret nicht mehr lesen.
    status["secret_readable"] = (
        decrypt_secret(user.totp_secret_encrypted) is not None if status["enabled"] else None
    )
    return status


def pending_setup_secret(user: User) -> str | None:
    """Secret einer begonnenen, noch nicht bestätigten Einrichtung."""
    if two_factor_enabled(user):
        return None
    return decrypt_secret(user.totp_secret_encrypted)


# ---------------------------------------------------------------------------
# Wiederherstellungscodes
# ---------------------------------------------------------------------------


def _generate_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return "-".join(raw[index : index + 4] for index in range(0, RECOVERY_CODE_LENGTH, 4))


def _normalize_code(code: str | None) -> str:
    return "".join((code or "").split()).replace("-", "").upper()


def _hash_recovery_code(user_id: int, normalized: str) -> str:
    return hashlib.sha256(f"{user_id}:{normalized}".encode()).hexdigest()


def _replace_recovery_codes(session: Session, user: User) -> list[str]:
    session.execute(delete(TwoFactorRecoveryCode).where(TwoFactorRecoveryCode.user_id == user.id))
    codes = [_generate_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    session.add_all(
        TwoFactorRecoveryCode(
            user_id=user.id, code_hash=_hash_recovery_code(user.id, _normalize_code(code))
        )
        for code in codes
    )
    return codes


def _remaining_recovery_codes(session: Session, user: User) -> int:
    count = session.scalar(
        select(func.count(TwoFactorRecoveryCode.id)).where(
            TwoFactorRecoveryCode.user_id == user.id,
            TwoFactorRecoveryCode.used_at.is_(None),
        )
    )
    return int(count or 0)


# ---------------------------------------------------------------------------
# Code prüfen und verbrauchen
# ---------------------------------------------------------------------------


def _is_totp_format(normalized: str) -> bool:
    return len(normalized) == TOTP_DIGITS and normalized.isascii() and normalized.isdigit()


def _consume_step(session: Session, user: User, step: int) -> bool:
    """Bucht den Zeitschritt atomar ab; ``False``, wenn er schon verbraucht ist."""
    result = session.execute(
        update(User)
        .where(
            User.id == user.id,
            or_(User.totp_last_step.is_(None), User.totp_last_step < step),
        )
        .values(totp_last_step=step)
        .execution_options(synchronize_session=False)
    )
    session.expire(user, ["totp_last_step"])
    return bool(result.rowcount)


def _consume_recovery_code(session: Session, user: User, normalized: str) -> bool:
    result = session.execute(
        update(TwoFactorRecoveryCode)
        .where(
            TwoFactorRecoveryCode.user_id == user.id,
            TwoFactorRecoveryCode.code_hash == _hash_recovery_code(user.id, normalized),
            TwoFactorRecoveryCode.used_at.is_(None),
        )
        .values(used_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


def verify_code(
    session: Session, user: User, code: str | None, *, at: float | None = None
) -> str | None:
    """Prüft TOTP- oder Wiederherstellungscode und verbraucht ihn.

    Liefert :data:`METHOD_TOTP`, :data:`METHOD_RECOVERY_CODE` oder ``None``.
    """
    if not two_factor_enabled(user):
        return None
    normalized = _normalize_code(code)
    if _is_totp_format(normalized):
        secret = decrypt_secret(user.totp_secret_encrypted)
        if secret is None:
            return None
        step = matching_step(secret, normalized, at=at)
        if step is None or not _consume_step(session, user, step):
            return None
        return METHOD_TOTP
    if len(normalized) == RECOVERY_CODE_LENGTH and _consume_recovery_code(
        session, user, normalized
    ):
        return METHOD_RECOVERY_CODE
    return None


def record_verified_login(
    session: Session, user: User, *, method: str, payload: dict[str, Any]
) -> int | None:
    """Protokolliert den bestandenen zweiten Faktor; liefert die verbleibenden Codes.

    Rückgabe nur nach einem eingesetzten Wiederherstellungscode, sonst ``None``.
    """
    record_security_event(
        session,
        user=user,
        action="two_factor_verified",
        actor=user.username,
        payload={**payload, "method": method},
    )
    if method != METHOD_RECOVERY_CODE:
        return None
    remaining = _remaining_recovery_codes(session, user)
    record_security_event(
        session,
        user=user,
        action="two_factor_recovery_code_used",
        actor=user.username,
        payload={**payload, "remaining": remaining},
    )
    return remaining


# ---------------------------------------------------------------------------
# Einrichten, Abschalten, Zurücksetzen
# ---------------------------------------------------------------------------


def _require_password(
    session: Session, user: User, password: str | None, *, actor: str, action: str
) -> None:
    if not check_password_hash(user.password_hash, password or ""):
        # Nur ins App-Log: Die Fehlversuche begrenzt der Aufrufer über das Rate-Limit.
        record_security_event(session, user=user, action=action, actor=actor, audit=False)
        raise TwoFactorCredentialsError(PASSWORD_MESSAGE)


def start_setup(session: Session, user: User, *, password: str | None, actor: str) -> str:
    """Beginnt die Einrichtung und liefert das neue Secret; noch nicht aktiv.

    Das Passwort schützt davor, dass eine fremde Session oder ein abgeflossenes API-Token
    den Benutzer durch Einschalten aus dem Browser-Login aussperrt.
    """
    if current_app.config.get("DEMO_MODE"):
        raise TwoFactorError(DEMO_MESSAGE)
    _require_password(session, user, password, actor=actor, action="two_factor_setup_failed")
    if two_factor_enabled(user):
        raise TwoFactorError("Die Zwei-Faktor-Anmeldung ist bereits aktiv.")
    secret = generate_secret()
    user.totp_secret_encrypted = encrypt_secret(secret)
    user.totp_last_step = None
    record_security_event(
        session, user=user, action="two_factor_setup_started", actor=actor, audit=False
    )
    return secret


def confirm_setup(
    session: Session, user: User, *, code: str | None, actor: str, at: float | None = None
) -> list[str]:
    """Aktiviert nach gültigem Code und liefert die Wiederherstellungscodes (nur dieses Mal)."""
    if current_app.config.get("DEMO_MODE"):
        raise TwoFactorError(DEMO_MESSAGE)
    if two_factor_enabled(user):
        raise TwoFactorError("Die Zwei-Faktor-Anmeldung ist bereits aktiv.")
    secret = pending_setup_secret(user)
    if secret is None:
        raise TwoFactorError("Bitte beginnen Sie die Einrichtung neu.")
    normalized = _normalize_code(code)
    step = matching_step(secret, normalized, at=at) if _is_totp_format(normalized) else None
    if step is None:
        record_security_event(
            session, user=user, action="two_factor_setup_failed", actor=actor, audit=False
        )
        raise TwoFactorCredentialsError(
            "Der Code ist ungültig. Bitte prüfen Sie auch die Uhrzeit Ihres Geräts."
        )
    user.totp_enabled_at = datetime.now(timezone.utc)
    user.totp_last_step = step
    codes = _replace_recovery_codes(session, user)
    record_security_event(
        session,
        user=user,
        action="two_factor_enabled",
        actor=actor,
        payload={"recovery_codes": len(codes)},
    )
    return codes


def _require_active(user: User) -> None:
    if not two_factor_enabled(user):
        raise TwoFactorError("Die Zwei-Faktor-Anmeldung ist nicht aktiv.")


def _verify_for_change(
    session: Session,
    user: User,
    *,
    password: str | None,
    code: str | None,
    actor: str,
    failed_action: str,
    at: float | None = None,
) -> str:
    _require_active(user)
    _require_password(session, user, password, actor=actor, action=failed_action)
    method = verify_code(session, user, code, at=at)
    if method is None:
        record_security_event(session, user=user, action=failed_action, actor=actor, audit=False)
        raise TwoFactorCredentialsError(INVALID_CODE_MESSAGE)
    return method


def _clear_two_factor(session: Session, user: User) -> None:
    user.totp_secret_encrypted = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    session.execute(delete(TwoFactorRecoveryCode).where(TwoFactorRecoveryCode.user_id == user.id))


def disable_two_factor(
    session: Session,
    user: User,
    *,
    password: str | None,
    code: str | None,
    actor: str,
    at: float | None = None,
) -> None:
    """Schaltet ab: Passwort plus aktueller Code oder Wiederherstellungscode."""
    method = _verify_for_change(
        session,
        user,
        password=password,
        code=code,
        actor=actor,
        failed_action="two_factor_disable_failed",
        at=at,
    )
    _clear_two_factor(session, user)
    record_security_event(
        session, user=user, action="two_factor_disabled", actor=actor, payload={"method": method}
    )


def regenerate_recovery_codes(
    session: Session,
    user: User,
    *,
    password: str | None,
    code: str | None,
    actor: str,
    at: float | None = None,
) -> list[str]:
    """Ersetzt alle Wiederherstellungscodes: Passwort plus aktueller Code."""
    method = _verify_for_change(
        session,
        user,
        password=password,
        code=code,
        actor=actor,
        failed_action="two_factor_recovery_codes_failed",
        at=at,
    )
    codes = _replace_recovery_codes(session, user)
    record_security_event(
        session,
        user=user,
        action="two_factor_recovery_codes_regenerated",
        actor=actor,
        payload={"method": method, "recovery_codes": len(codes)},
    )
    return codes


def reset_two_factor(session: Session, user: User, *, actor: str) -> bool:
    """Administrator setzt zurück (Gerät verloren, ``SECRET_KEY`` rotiert).

    Liefert ``False``, wenn nichts zurückzusetzen war.
    """
    was_enabled = two_factor_enabled(user)
    if not was_enabled and user.totp_secret_encrypted is None:
        return False
    _clear_two_factor(session, user)
    record_security_event(
        session,
        user=user,
        action="two_factor_reset",
        actor=actor,
        payload={"was_enabled": was_enabled},
    )
    return True
