"""Geschäftspartner-Stammdaten (Debitoren/Kreditoren).

Partner hängen als Nebenbuch an Buchungszeilen auf Sammelkonten. Die Rolle
ergibt sich aus der Personenkontonummer: Kunde = Debitorennummer
(10000–69999), Lieferant = Kreditorennummer (70000–99999) – DATEV-Standard
bei vierstelligen Sachkonten. Änderungen werden mit vollständigem Vorher-/
Nachher-Snapshot protokolliert; Bankdaten nur über ``set_partner_bank_details``.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event, serialize_audit_log_entry
from app.services.sepa_export import SepaExportError, normalize_bic, normalize_iban
from domain.models import (
    PARTNER_KIND_ORGANIZATION,
    PARTNER_KINDS,
    SUBLEDGER_CREDITOR,
    SUBLEDGER_DEBTOR,
    Account,
    AuditLog,
    BusinessPartner,
    Company,
    JournalEntryLine,
)

NUMBER_RANGES = {
    SUBLEDGER_DEBTOR: (10000, 69999),
    SUBLEDGER_CREDITOR: (70000, 99999),
}
NUMBER_FIELDS = {
    SUBLEDGER_DEBTOR: "debtor_number",
    SUBLEDGER_CREDITOR: "creditor_number",
}
ROLE_LABELS = {SUBLEDGER_DEBTOR: "Kunde", SUBLEDGER_CREDITOR: "Lieferant"}

# Felder, die über create/update geändert werden dürfen (ohne Rollen/Bankdaten).
TEXT_FIELD_LIMITS = {
    "name": 255,
    "street": 255,
    "postal_code": 20,
    "city": 120,
    "tax_number": 30,
    "email": 255,
    "phone": 50,
    "contact_person": 255,
}
UPDATABLE_FIELDS = (
    *TEXT_FIELD_LIMITS,
    "partner_kind",
    "country_code",
    "vat_id",
    "payment_term_days",
    "notes",
    "is_active",
    "is_customer",
    "is_supplier",
    "debtor_number",
    "creditor_number",
)

NUMBER_ATTEMPTS = 5
_UNSET: Any = object()
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_VAT_ID_RE = re.compile(r"^[A-Z]{2}[0-9A-Z+*.]{2,12}$")
_VAT_ID_DE_RE = re.compile(r"^DE[0-9]{9}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class PartnerError(ValueError):
    """Ungültige Geschäftspartner-Stammdaten oder Zuordnung."""


def serialize_partner(partner: BusinessPartner) -> dict[str, Any]:
    return {
        "id": partner.id,
        "tenant_id": partner.tenant_id,
        "company_id": partner.company_id,
        "debtor_number": partner.debtor_number,
        "creditor_number": partner.creditor_number,
        "is_customer": partner.debtor_number is not None,
        "is_supplier": partner.creditor_number is not None,
        "partner_kind": partner.partner_kind,
        "name": partner.name,
        "street": partner.street,
        "postal_code": partner.postal_code,
        "city": partner.city,
        "country_code": partner.country_code,
        "vat_id": partner.vat_id,
        "tax_number": partner.tax_number,
        "email": partner.email,
        "phone": partner.phone,
        "contact_person": partner.contact_person,
        "iban": partner.iban,
        "bic": partner.bic,
        "payment_term_days": partner.payment_term_days,
        "notes": partner.notes,
        "is_active": partner.is_active,
        "created_at": partner.created_at.isoformat() if partner.created_at else None,
    }


def partner_display_number(partner: BusinessPartner, subledger: str | None = None) -> str:
    """Personenkontonummer passend zur Sammelkontoseite (sonst die erste vorhandene)."""
    if subledger == SUBLEDGER_CREDITOR and partner.creditor_number:
        return partner.creditor_number
    if subledger == SUBLEDGER_DEBTOR and partner.debtor_number:
        return partner.debtor_number
    return partner.debtor_number or partner.creditor_number or ""


# ---------------------------------------------------------------------------
# Normalisierung und Validierung
# ---------------------------------------------------------------------------


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PartnerError(f"{field} muss ein Text sein.")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    limit = TEXT_FIELD_LIMITS.get(field)
    if limit is not None and len(normalized) > limit:
        raise PartnerError(f"{field} darf höchstens {limit} Zeichen lang sein.")
    return normalized


def _required_name(value: Any) -> str:
    name = _optional_text(value, "name")
    if not name:
        raise PartnerError("Name ist Pflicht.")
    return name


def _partner_kind(value: Any) -> str:
    normalized = (value or PARTNER_KIND_ORGANIZATION)
    if not isinstance(normalized, str) or normalized.strip() not in PARTNER_KINDS:
        raise PartnerError("partner_kind muss organization oder person sein.")
    return normalized.strip()


def _country_code(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "DE"
    if not isinstance(value, str):
        raise PartnerError("country_code muss ein ISO-Ländercode sein.")
    normalized = value.strip().upper()
    if not _COUNTRY_RE.match(normalized):
        raise PartnerError("country_code muss ein zweistelliger ISO-Ländercode sein (z. B. DE).")
    return normalized


def normalize_vat_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PartnerError("vat_id muss ein Text sein.")
    normalized = "".join(value.split()).upper()
    if not normalized:
        return None
    if normalized.startswith("DE"):
        if not _VAT_ID_DE_RE.match(normalized):
            raise PartnerError(
                f"Ungültige USt-IdNr {value}: deutsche Nummern bestehen aus DE und 9 Ziffern."
            )
    elif not _VAT_ID_RE.match(normalized):
        raise PartnerError(
            f"Ungültige USt-IdNr {value}: erwartet Länderpräfix und 2–12 Zeichen."
        )
    return normalized


def _email(value: Any) -> str | None:
    email = _optional_text(value, "email")
    if email is not None and not _EMAIL_RE.match(email):
        raise PartnerError(f"Ungültige E-Mail-Adresse: {email}")
    return email


def _payment_term_days(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise PartnerError("payment_term_days muss eine ganze Zahl sein.")
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise PartnerError("payment_term_days muss eine ganze Zahl sein.") from exc
    if days < 0 or days > 365:
        raise PartnerError("payment_term_days muss zwischen 0 und 365 liegen.")
    return days


def _notes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PartnerError("notes muss ein Text sein.")
    return value.strip() or None


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise PartnerError(f"{field} muss true oder false sein.")
    return value


def _partner_number(value: Any, role: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    raw = str(value).strip()
    low, high = NUMBER_RANGES[role]
    if not raw.isdigit() or not low <= int(raw) <= high:
        label = "Debitorennummer" if role == SUBLEDGER_DEBTOR else "Kreditorennummer"
        raise PartnerError(f"{label} muss im Bereich {low}–{high} liegen.")
    return str(int(raw))


def _iban(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PartnerError("iban muss ein Text sein.")
    try:
        return normalize_iban(value)
    except SepaExportError as exc:
        raise PartnerError(str(exc)) from exc


def _bic(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PartnerError("bic muss ein Text sein.")
    try:
        return normalize_bic(value)
    except SepaExportError as exc:
        raise PartnerError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Nummernkreise
# ---------------------------------------------------------------------------


def next_partner_number(*, session: Session, company_id: int, role: str) -> str:
    """Nächste freie Personenkontonummer im Bereich der Rolle."""
    column = getattr(BusinessPartner, NUMBER_FIELDS[role])
    low, high = NUMBER_RANGES[role]
    # Kein Autoflush: ein gerade geänderter Partner kann noch unvollständig sein.
    with session.no_autoflush:
        numbers = session.execute(
            select(column).where(BusinessPartner.company_id == company_id, column.is_not(None))
        ).scalars()
        used = [
            int(number) for number in numbers if number.isdigit() and low <= int(number) <= high
        ]
    candidate = max(used) + 1 if used else low
    if candidate > high:
        raise PartnerError(f"Nummernkreis {low}–{high} ist ausgeschöpft.")
    return str(candidate)


def _role_in_use(session: Session, partner: BusinessPartner, role: str) -> bool:
    if partner.id is None:
        return False
    with session.no_autoflush:
        return (
            session.execute(
                select(JournalEntryLine.id)
                .join(Account, Account.id == JournalEntryLine.account_id)
                .where(
                    JournalEntryLine.partner_id == partner.id,
                    Account.subledger == role,
                )
                .limit(1)
            ).first()
            is not None
        )


def _apply_roles(
    *,
    session: Session,
    partner: BusinessPartner,
    is_customer: bool | object,
    is_supplier: bool | object,
    debtor_number: Any,
    creditor_number: Any,
) -> list[str]:
    """Setzt Rollen/Nummern; liefert die Rollen, deren Nummer automatisch zu vergeben ist."""
    pending: list[str] = []
    for role, wanted, raw_number in (
        (SUBLEDGER_DEBTOR, is_customer, debtor_number),
        (SUBLEDGER_CREDITOR, is_supplier, creditor_number),
    ):
        field = NUMBER_FIELDS[role]
        flag = "is_customer" if role == SUBLEDGER_DEBTOR else "is_supplier"
        current = getattr(partner, field)
        requested = _partner_number(raw_number, role) if raw_number is not _UNSET else None
        if wanted is _UNSET or wanted is None:
            wanted = current is not None or requested is not None
        wanted = _bool(wanted, flag)
        if not wanted and requested is not None:
            raise PartnerError(f"{field} ist angegeben, aber {flag} ist false.")

        if not wanted:
            if current is not None:
                if _role_in_use(session, partner, role):
                    raise PartnerError(
                        f"Die Rolle {ROLE_LABELS[role]} wird bereits in Buchungen verwendet "
                        "und kann nicht entfernt werden."
                    )
                setattr(partner, field, None)
            continue

        if requested is not None and requested != current:
            if current is not None and _role_in_use(session, partner, role):
                raise PartnerError(
                    f"Die Personenkontonummer {current} wird bereits in Buchungen "
                    "verwendet und ist unveränderlich."
                )
            setattr(partner, field, requested)
        elif current is None:
            pending.append(role)
    return pending


def _flush_with_numbers(
    *, session: Session, partner: BusinessPartner, pending_roles: list[str]
) -> None:
    """Vergibt automatische Nummern und speichert.

    Bei einer Kollision (Wettlauf zweier Anlagen) wird für neue Partner neu
    gezogen; bei Änderungen bricht der Vorgang ab, damit keine bereits
    gesetzten Werte durch den Savepoint-Rollback verloren gehen.
    """
    retry = partner.id is None and bool(pending_roles)
    for attempt in range(1, NUMBER_ATTEMPTS + 1):
        for role in pending_roles:
            setattr(
                partner,
                NUMBER_FIELDS[role],
                next_partner_number(session=session, company_id=partner.company_id, role=role),
            )
        try:
            with session.begin_nested():
                session.add(partner)
                session.flush()
        except IntegrityError as exc:
            if not retry or attempt == NUMBER_ATTEMPTS:
                raise PartnerError(
                    "Personenkontonummer ist in dieser Gesellschaft bereits vergeben."
                ) from exc
            continue
        return


# ---------------------------------------------------------------------------
# Anlage, Änderung, Bankdaten
# ---------------------------------------------------------------------------


def duplicate_hints(
    *,
    session: Session,
    company_id: int,
    name: str | None = None,
    vat_id: str | None = None,
    iban: str | None = None,
    exclude_id: int | None = None,
) -> list[str]:
    """Hinweise (nicht blockierend) auf mögliche Dubletten."""
    hints: list[str] = []
    stmt = select(BusinessPartner).where(BusinessPartner.company_id == company_id)
    if exclude_id is not None:
        stmt = stmt.where(BusinessPartner.id != exclude_id)
    normalized_name = " ".join((name or "").lower().split())
    for other in session.execute(stmt).scalars():
        label = f"{partner_display_number(other)} {other.name}".strip()
        if vat_id and other.vat_id == vat_id:
            hints.append(f"USt-IdNr {vat_id} ist bereits bei {label} hinterlegt.")
        if iban and other.iban == iban:
            hints.append(f"IBAN {iban} ist bereits bei {label} hinterlegt.")
        if normalized_name and " ".join(other.name.lower().split()) == normalized_name:
            hints.append(f"Ein Partner mit dem Namen „{other.name}“ existiert bereits ({label}).")
    return hints


def create_partner(
    *,
    session: Session,
    company: Company,
    changed_by: str,
    name: Any = None,
    is_customer: Any = None,
    is_supplier: Any = None,
    debtor_number: Any = None,
    creditor_number: Any = None,
    partner_kind: Any = PARTNER_KIND_ORGANIZATION,
    street: Any = None,
    postal_code: Any = None,
    city: Any = None,
    country_code: Any = "DE",
    vat_id: Any = None,
    tax_number: Any = None,
    email: Any = None,
    phone: Any = None,
    contact_person: Any = None,
    payment_term_days: Any = None,
    notes: Any = None,
    is_active: Any = True,
) -> BusinessPartner:
    partner = BusinessPartner(
        tenant_id=company.tenant_id,
        company_id=company.id,
        name=_required_name(name),
        partner_kind=_partner_kind(partner_kind),
        street=_optional_text(street, "street"),
        postal_code=_optional_text(postal_code, "postal_code"),
        city=_optional_text(city, "city"),
        country_code=_country_code(country_code),
        vat_id=normalize_vat_id(vat_id),
        tax_number=_optional_text(tax_number, "tax_number"),
        email=_email(email),
        phone=_optional_text(phone, "phone"),
        contact_person=_optional_text(contact_person, "contact_person"),
        payment_term_days=_payment_term_days(payment_term_days),
        notes=_notes(notes),
        is_active=_bool(is_active, "is_active"),
    )
    pending = _apply_roles(
        session=session,
        partner=partner,
        is_customer=is_customer,
        is_supplier=is_supplier,
        debtor_number=debtor_number,
        creditor_number=creditor_number,
    )
    if partner.debtor_number is None and partner.creditor_number is None and not pending:
        raise PartnerError("Ein Geschäftspartner braucht mindestens eine Rolle (Kunde/Lieferant).")
    _flush_with_numbers(session=session, partner=partner, pending_roles=pending)
    log_audit_event(
        session=session,
        tenant_id=partner.tenant_id,
        company_id=partner.company_id,
        entity_type="business_partner",
        entity_id=str(partner.id),
        action="created",
        changed_by=changed_by,
        payload={"before": None, "after": serialize_partner(partner)},
    )
    return partner


def update_partner(
    *,
    session: Session,
    partner: BusinessPartner,
    changed_by: str,
    **changes: Any,
) -> bool:
    """Ändert Stammdaten (ohne Bankdaten); liefert False bei einem No-op."""
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise PartnerError(
            f"Nicht änderbare Felder: {', '.join(sorted(unknown))} "
            "(Bankdaten über den Bankdaten-Endpunkt)."
        )
    if not changes:
        raise PartnerError("Mindestens ein änderbares Feld ist erforderlich.")
    before = serialize_partner(partner)

    if "name" in changes:
        partner.name = _required_name(changes["name"])
    for field in ("street", "postal_code", "city", "tax_number", "phone", "contact_person"):
        if field in changes:
            setattr(partner, field, _optional_text(changes[field], field))
    if "email" in changes:
        partner.email = _email(changes["email"])
    if "partner_kind" in changes:
        partner.partner_kind = _partner_kind(changes["partner_kind"])
    if "country_code" in changes:
        partner.country_code = _country_code(changes["country_code"])
    if "vat_id" in changes:
        partner.vat_id = normalize_vat_id(changes["vat_id"])
    if "payment_term_days" in changes:
        partner.payment_term_days = _payment_term_days(changes["payment_term_days"])
    if "notes" in changes:
        partner.notes = _notes(changes["notes"])
    if "is_active" in changes:
        partner.is_active = _bool(changes["is_active"], "is_active")

    pending: list[str] = []
    if {"is_customer", "is_supplier", "debtor_number", "creditor_number"} & set(changes):
        pending = _apply_roles(
            session=session,
            partner=partner,
            is_customer=changes.get("is_customer", _UNSET),
            is_supplier=changes.get("is_supplier", _UNSET),
            debtor_number=changes.get("debtor_number", _UNSET),
            creditor_number=changes.get("creditor_number", _UNSET),
        )
        if partner.debtor_number is None and partner.creditor_number is None and not pending:
            raise PartnerError(
                "Ein Geschäftspartner braucht mindestens eine Rolle (Kunde/Lieferant)."
            )
    _flush_with_numbers(session=session, partner=partner, pending_roles=pending)

    after = serialize_partner(partner)
    if before == after:
        return False
    log_audit_event(
        session=session,
        tenant_id=partner.tenant_id,
        company_id=partner.company_id,
        entity_type="business_partner",
        entity_id=str(partner.id),
        action="updated",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


def set_partner_bank_details(
    *,
    session: Session,
    partner: BusinessPartner,
    changed_by: str,
    iban: Any,
    bic: Any = None,
) -> bool:
    """Setzt die Bankverbindung; leere Werte löschen sie. Eigene Audit-Aktion."""
    before = serialize_partner(partner)
    partner.iban = _iban(iban)
    partner.bic = _bic(bic)
    if partner.bic and not partner.iban:
        raise PartnerError("Eine BIC ohne IBAN ist nicht zulässig.")
    after = serialize_partner(partner)
    if before == after:
        return False
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=partner.tenant_id,
        company_id=partner.company_id,
        entity_type="business_partner",
        entity_id=str(partner.id),
        action="bank_details_changed",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


def partner_history(
    *, session: Session, partner_id: int, limit: int = 100
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(limit, 500))
    entries = (
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "business_partner",
                AuditLog.entity_id == str(partner_id),
            )
            .order_by(AuditLog.changed_at.desc(), AuditLog.id.desc())
            .limit(bounded_limit)
        )
        .scalars()
        .all()
    )
    return [serialize_audit_log_entry(entry) for entry in entries]


def list_partners(
    *,
    session: Session,
    company_id: int,
    role: str | None = None,
    query: str | None = None,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[BusinessPartner], int]:
    stmt = select(BusinessPartner).where(BusinessPartner.company_id == company_id)
    if role == SUBLEDGER_DEBTOR:
        stmt = stmt.where(BusinessPartner.debtor_number.is_not(None))
    elif role == SUBLEDGER_CREDITOR:
        stmt = stmt.where(BusinessPartner.creditor_number.is_not(None))
    elif role is not None:
        raise PartnerError("role muss debtor oder creditor sein.")
    if not include_inactive:
        stmt = stmt.where(BusinessPartner.is_active.is_(True))
    if query:
        pattern = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(
                BusinessPartner.name.ilike(pattern),
                BusinessPartner.debtor_number.ilike(pattern),
                BusinessPartner.creditor_number.ilike(pattern),
                BusinessPartner.vat_id.ilike(pattern),
                BusinessPartner.city.ilike(pattern),
            )
        )
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    partners = (
        session.execute(
            stmt.order_by(BusinessPartner.name, BusinessPartner.id).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    return list(partners), total


# ---------------------------------------------------------------------------
# Nebenbuch: Partner an Buchungszeilen
# ---------------------------------------------------------------------------


def find_partner_by_number(
    *, session: Session, company_id: int, number: str
) -> BusinessPartner | None:
    normalized = (number or "").strip()
    if not normalized:
        return None
    return session.execute(
        select(BusinessPartner).where(
            BusinessPartner.company_id == company_id,
            or_(
                BusinessPartner.debtor_number == normalized,
                BusinessPartner.creditor_number == normalized,
            ),
        )
    ).scalar_one_or_none()


def validate_partner_assignment(
    *,
    session: Session,
    company_id: int,
    partner_id: int | None,
    account: Account,
    allow_inactive: bool = False,
) -> BusinessPartner | None:
    """Prüft einen Partner an einer Buchungszeile (Sammelkonto, Rolle, Status)."""
    if partner_id is None:
        return None
    partner = session.get(BusinessPartner, partner_id)
    if partner is None or partner.company_id != company_id:
        raise PartnerError("Geschäftspartner für Buchungszeile nicht gefunden.")
    if account.subledger is None:
        raise PartnerError(
            f"Konto {account.code} ist kein Debitoren-/Kreditoren-Sammelkonto; "
            "ein Geschäftspartner ist nur auf Sammelkonten zulässig."
        )
    if getattr(partner, NUMBER_FIELDS[account.subledger]) is None:
        side = "Debitoren" if account.subledger == SUBLEDGER_DEBTOR else "Kreditoren"
        raise PartnerError(
            f"{partner.name} ist kein {ROLE_LABELS[account.subledger]} und kann nicht auf "
            f"dem {side}-Sammelkonto {account.code} gebucht werden."
        )
    if not partner.is_active and not allow_inactive:
        raise PartnerError(f"{partner.name} ist inaktiv und darf nicht bebucht werden.")
    return partner
