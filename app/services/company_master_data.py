"""Firmenstammdaten: Anschrift, Steuer-, Register- und Kontaktangaben einer Gesellschaft.

Rechnungen, E-Rechnungs-Export und Mahnschreiben nutzen diese Angaben. Die Umgebungsvariablen
``SELLER_*`` bleiben Rückfallwert für leere Felder. Änderungen stehen mit Vorher/Nachher im
Audit-Log. Die IBAN wird hier nur gelesen; geändert wird sie über ``set_company_bank_details``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from flask import current_app, has_app_context
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.einvoice_export import Party
from app.services.partners import PartnerError, normalize_vat_id
from domain.models import BusinessPartner, Company

DEFAULT_INVOICE_PREFIX = "RE"
DEFAULT_COUNTRY_CODE = "DE"

TEXT_FIELD_LIMITS = {
    "street": 255,
    "postal_code": 20,
    "city": 120,
    "tax_number": 30,
    "phone": 50,
    "website": 255,
    "register_court": 120,
    "register_number": 50,
    "managing_directors": 255,
    "bank_name": 120,
}
UPDATABLE_FIELDS = (
    *TEXT_FIELD_LIMITS,
    "country_code",
    "vat_id",
    "email",
    "invoice_prefix",
    "is_small_business",
)

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


class CompanyMasterDataError(ValueError):
    """Ungültige Firmenstammdaten."""


def serialize_company_master_data(company: Company) -> dict[str, Any]:
    return {
        "company_id": company.id,
        "name": company.name,
        "currency_code": company.currency_code,
        "street": company.street,
        "postal_code": company.postal_code,
        "city": company.city,
        "country_code": company.country_code,
        "vat_id": company.vat_id,
        "tax_number": company.tax_number,
        "phone": company.phone,
        "email": company.email,
        "website": company.website,
        "register_court": company.register_court,
        "register_number": company.register_number,
        "managing_directors": company.managing_directors,
        "bank_name": company.bank_name,
        "invoice_prefix": company.invoice_prefix,
        "is_small_business": company.is_small_business,
        # Nur lesend: geändert über set_company_bank_details.
        "iban": company.iban,
        "bic": company.bic,
    }


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CompanyMasterDataError(f"{field} muss ein Text sein.")
    normalized = " ".join(value.split())
    if not normalized:
        return None
    limit = TEXT_FIELD_LIMITS.get(field)
    if limit is not None and len(normalized) > limit:
        raise CompanyMasterDataError(f"{field} darf höchstens {limit} Zeichen lang sein.")
    return normalized


def _country_code(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise CompanyMasterDataError("country_code muss ein ISO-Ländercode sein.")
    normalized = value.strip().upper()
    if not _COUNTRY_RE.match(normalized):
        raise CompanyMasterDataError(
            "country_code muss ein zweistelliger ISO-Ländercode sein (z. B. DE)."
        )
    return normalized


def _vat_id(value: Any) -> str | None:
    try:
        return normalize_vat_id(value)
    except PartnerError as exc:
        raise CompanyMasterDataError(str(exc)) from exc


def _email(value: Any) -> str | None:
    email = _optional_text(value, "email")
    if email is not None and not _EMAIL_RE.match(email):
        raise CompanyMasterDataError(f"Ungültige E-Mail-Adresse: {email}")
    return email


def _invoice_prefix(value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_INVOICE_PREFIX
    if not isinstance(value, str) or not _PREFIX_RE.match(value.strip()):
        raise CompanyMasterDataError(
            "invoice_prefix darf nur aus 1 bis 10 Buchstaben und Ziffern bestehen."
        )
    return value.strip()


def update_company_master_data(
    *, session: Session, company: Company, changed_by: str, **changes: Any
) -> bool:
    """Ändert Stammdaten (ohne Bankdaten); liefert False bei einem No-op. Ohne Commit."""
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise CompanyMasterDataError(
            f"Nicht änderbare Felder: {', '.join(sorted(unknown))} "
            "(Bankdaten über den Bankdaten-Endpunkt)."
        )
    if not changes:
        raise CompanyMasterDataError("Mindestens ein änderbares Feld ist erforderlich.")
    before = serialize_company_master_data(company)

    for field in TEXT_FIELD_LIMITS:
        if field in changes:
            setattr(company, field, _optional_text(changes[field], field))
    if "country_code" in changes:
        company.country_code = _country_code(changes["country_code"])
    if "vat_id" in changes:
        company.vat_id = _vat_id(changes["vat_id"])
    if "email" in changes:
        company.email = _email(changes["email"])
    if "invoice_prefix" in changes:
        company.invoice_prefix = _invoice_prefix(changes["invoice_prefix"])
    if "is_small_business" in changes:
        if not isinstance(changes["is_small_business"], bool):
            raise CompanyMasterDataError("is_small_business muss true oder false sein.")
        company.is_small_business = changes["is_small_business"]

    after = serialize_company_master_data(company)
    if before == after:
        return False
    session.flush()
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company",
        entity_id=str(company.id),
        action="master_data_updated",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


@dataclass(frozen=True, slots=True)
class SellerData:
    """Aussteller einer Rechnung: Firmenstammdaten, leere Felder aus ``SELLER_*``."""

    name: str
    street: str
    postal_code: str
    city: str
    country_code: str
    vat_id: str
    tax_number: str
    phone: str
    email: str
    website: str
    register_court: str
    register_number: str
    managing_directors: str
    bank_name: str
    iban: str
    bic: str

    @property
    def postal_line(self) -> str:
        return " ".join(part for part in (self.postal_code, self.city) if part)

    @property
    def register_text(self) -> str:
        """Registerangabe für den Rechnungsfuß, z. B. ``Amtsgericht Berlin HRB 12345``."""
        return " ".join(part for part in (self.register_court, self.register_number) if part)

    @property
    def sender_line(self) -> str:
        """Absenderzeile im Sichtfenster eines Schreibens."""
        return " · ".join(part for part in (self.name, self.street, self.postal_line) if part)


def seller_fallbacks(config: Mapping[str, Any] | None = None) -> dict[str, str]:
    """``SELLER_*`` aus der App-Konfiguration; außerhalb eines App-Kontexts leer."""
    if config is None:
        if not has_app_context():
            return {}
        config = current_app.config
    return {
        "street": config.get("SELLER_STREET") or "",
        "postal_code": config.get("SELLER_POSTAL_CODE") or "",
        "city": config.get("SELLER_CITY") or "",
        "country_code": config.get("SELLER_COUNTRY_CODE") or "",
        "vat_id": config.get("SELLER_VAT_ID") or "",
    }


def resolve_seller(company: Company, config: Mapping[str, Any] | None = None) -> SellerData:
    """Firmenstammdaten mit den ``SELLER_*``-Werten als Rückfall für leere Felder."""
    fallbacks = seller_fallbacks(config)
    return SellerData(
        name=company.name,
        street=company.street or fallbacks.get("street", ""),
        postal_code=company.postal_code or fallbacks.get("postal_code", ""),
        city=company.city or fallbacks.get("city", ""),
        country_code=company.country_code
        or fallbacks.get("country_code", "")
        or DEFAULT_COUNTRY_CODE,
        vat_id=company.vat_id or fallbacks.get("vat_id", ""),
        tax_number=company.tax_number or "",
        phone=company.phone or "",
        email=company.email or "",
        website=company.website or "",
        register_court=company.register_court or "",
        register_number=company.register_number or "",
        managing_directors=company.managing_directors or "",
        bank_name=company.bank_name or "",
        iban=company.iban or "",
        bic=company.bic or "",
    )


def partner_party(partner: BusinessPartner, *, overrides: Party | None = None) -> Party:
    """Käufer einer E-Rechnung aus dem Partnerstamm; ausgefüllte Angaben gehen vor."""
    given = overrides or Party(name="", country_code="")
    return Party(
        name=given.name or partner.name,
        street=given.street or partner.street or "",
        postal_code=given.postal_code or partner.postal_code or "",
        city=given.city or partner.city or "",
        # Das Land hat einen Standardwert und zählt deshalb nur zusammen mit einer Anschrift.
        country_code=(given.country_code if given.street else "") or partner.country_code or "DE",
        vat_id=given.vat_id or partner.vat_id or "",
        contact_name=partner.contact_person or "",
        phone=partner.phone or "",
        email=partner.email or "",
    )


def seller_party(company: Company, config: Mapping[str, Any] | None = None) -> Party:
    """Verkäufer für die E-Rechnung aus den Firmenstammdaten."""
    seller = resolve_seller(company, config)
    return Party(
        name=seller.name,
        street=seller.street,
        postal_code=seller.postal_code,
        city=seller.city,
        country_code=seller.country_code,
        vat_id=seller.vat_id,
        tax_number=seller.tax_number,
        contact_name=seller.managing_directors or seller.name,
        phone=seller.phone,
        email=seller.email,
        legal_registration_id=seller.register_number,
        legal_form_text=seller.register_text,
    )
