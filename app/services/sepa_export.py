"""SEPA-Zahllauf: Überweisungsdatei pain.001.001.03 aus offenen Kreditoren-Posten.

Der Zahllauf erzeugt eine XML-Datei zum Upload ins Online-Banking; die
Ausführung bleibt bei der Bank. Die Posten werden dabei nicht ausgeglichen —
der Ausgleich passiert wie gewohnt über den Kontoauszugs-Import bzw. OPOS.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from xml.etree import ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from domain.models import Company, OpenItem

PAIN_NAMESPACE = "urn:iso:std:iso:20022:tech:xsd:pain.001.001.03"


class SepaExportError(ValueError):
    """Raised when a payment run cannot be built."""


@dataclass(slots=True)
class PaymentRunResult:
    xml_bytes: bytes
    file_name: str
    transaction_count: int
    control_sum: Decimal
    open_item_ids: list[int]


def normalize_iban(raw: str | None) -> str | None:
    """Validiert eine IBAN (Format + Mod-97-Prüfsumme) und normalisiert sie."""
    iban = (raw or "").replace(" ", "").upper()
    if not iban:
        return None
    if not (15 <= len(iban) <= 34) or not iban[:2].isalpha() or not iban[2:4].isdigit():
        raise SepaExportError(f"Ungültige IBAN: {raw}")
    rearranged = iban[4:] + iban[:4]
    try:
        numeric = int("".join(str(int(char, 36)) for char in rearranged))
    except ValueError as exc:
        raise SepaExportError(f"Ungültige IBAN: {raw}") from exc
    if numeric % 97 != 1:
        raise SepaExportError(f"IBAN-Prüfsumme falsch: {raw}")
    return iban


def normalize_bic(raw: str | None) -> str | None:
    bic = (raw or "").replace(" ", "").upper()
    if not bic:
        return None
    if len(bic) not in (8, 11) or not bic.isalnum():
        raise SepaExportError(f"Ungültige BIC: {raw}")
    return bic


def set_company_bank_details(
    *, session: Session, company_id: int, iban: str | None, bic: str | None, changed_by: str
) -> Company:
    company = session.get(Company, company_id)
    if company is None:
        raise SepaExportError("Gesellschaft nicht gefunden.")
    company.iban = normalize_iban(iban)
    company.bic = normalize_bic(bic)
    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="company",
        entity_id=str(company.id),
        action="bank_details_updated",
        changed_by=changed_by,
        payload={"iban": company.iban, "bic": company.bic},
    )
    session.commit()
    session.refresh(company)
    return company


def payable_items_for_run(*, session: Session, company_id: int) -> list[OpenItem]:
    """Offene Kreditoren-Posten mit hinterlegter IBAN, fällige zuerst."""
    return list(
        session.execute(
            select(OpenItem)
            .where(
                OpenItem.company_id == company_id,
                OpenItem.item_type == "payable",
                OpenItem.status == "open",
                OpenItem.counterparty_iban.is_not(None),
            )
            .order_by(OpenItem.due_date.is_(None), OpenItem.due_date, OpenItem.id)
        )
        .scalars()
        .all()
    )


def _text_element(parent: ET.Element, tag: str, text: str) -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _sanitize(text: str, max_length: int) -> str:
    """SEPA-Zeichensatz light: ersetzt kritische Zeichen, kürzt auf max_length."""
    cleaned = "".join(
        char if char.isalnum() or char in " /-?:().,'+" else " " for char in text
    )
    return " ".join(cleaned.split())[:max_length] or "-"


def create_payment_run(
    *,
    session: Session,
    company_id: int,
    open_item_ids: Sequence[int],
    execution_date: date | None = None,
    changed_by: str,
) -> PaymentRunResult:
    """Erzeugt eine pain.001-Datei für die gewählten Kreditoren-Posten."""
    company = session.get(Company, company_id)
    if company is None:
        raise SepaExportError("Gesellschaft nicht gefunden.")
    if not company.iban:
        raise SepaExportError(
            "Für die Gesellschaft ist keine Auftraggeber-IBAN hinterlegt."
        )
    ids = list(dict.fromkeys(open_item_ids))
    if not ids:
        raise SepaExportError("Kein offener Posten ausgewählt.")

    items = (
        session.execute(select(OpenItem).where(OpenItem.id.in_(ids)))
        .scalars()
        .all()
    )
    missing = set(ids) - {item.id for item in items}
    if missing:
        raise SepaExportError(f"Offener Posten nicht gefunden: {sorted(missing)}")
    for item in items:
        if item.company_id != company.id:
            raise SepaExportError(f"Posten {item.reference} gehört zu einer anderen Gesellschaft.")
        if item.item_type != "payable" or item.status != "open":
            raise SepaExportError(f"Posten {item.reference} ist kein offener Kreditoren-Posten.")
        if not item.counterparty_iban:
            raise SepaExportError(f"Posten {item.reference} hat keine Empfänger-IBAN.")
        if item.currency_code != "EUR":
            raise SepaExportError(
                f"Posten {item.reference}: SEPA-Überweisungen sind nur in EUR möglich."
            )

    execution_date = execution_date or date.today()
    if execution_date < date.today():
        raise SepaExportError("Das Ausführungsdatum darf nicht in der Vergangenheit liegen.")

    control_sum = sum((item.open_amount for item in items), Decimal("0.00"))
    message_id = f"OB-{company.id}-{uuid.uuid4().hex[:12].upper()}"

    root = ET.Element("Document", xmlns=PAIN_NAMESPACE)
    initiation = ET.SubElement(root, "CstmrCdtTrfInitn")

    header = ET.SubElement(initiation, "GrpHdr")
    _text_element(header, "MsgId", message_id)
    _text_element(
        header,
        "CreDtTm",
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    _text_element(header, "NbOfTxs", str(len(items)))
    _text_element(header, "CtrlSum", str(control_sum))
    initiating_party = ET.SubElement(header, "InitgPty")
    _text_element(initiating_party, "Nm", _sanitize(company.name, 70))

    payment_info = ET.SubElement(initiation, "PmtInf")
    _text_element(payment_info, "PmtInfId", message_id)
    _text_element(payment_info, "PmtMtd", "TRF")
    _text_element(payment_info, "NbOfTxs", str(len(items)))
    _text_element(payment_info, "CtrlSum", str(control_sum))
    payment_type = ET.SubElement(payment_info, "PmtTpInf")
    service_level = ET.SubElement(payment_type, "SvcLvl")
    _text_element(service_level, "Cd", "SEPA")
    _text_element(payment_info, "ReqdExctnDt", execution_date.isoformat())
    debtor = ET.SubElement(payment_info, "Dbtr")
    _text_element(debtor, "Nm", _sanitize(company.name, 70))
    debtor_account = ET.SubElement(payment_info, "DbtrAcct")
    debtor_account_id = ET.SubElement(debtor_account, "Id")
    _text_element(debtor_account_id, "IBAN", company.iban)
    debtor_agent = ET.SubElement(payment_info, "DbtrAgt")
    debtor_fin = ET.SubElement(debtor_agent, "FinInstnId")
    if company.bic:
        _text_element(debtor_fin, "BIC", company.bic)
    else:
        other = ET.SubElement(debtor_fin, "Othr")
        _text_element(other, "Id", "NOTPROVIDED")
    _text_element(payment_info, "ChrgBr", "SLEV")

    for item in items:
        transfer = ET.SubElement(payment_info, "CdtTrfTxInf")
        payment_id = ET.SubElement(transfer, "PmtId")
        _text_element(payment_id, "EndToEndId", _sanitize(item.reference, 35))
        amount = ET.SubElement(transfer, "Amt")
        instructed = ET.SubElement(amount, "InstdAmt", Ccy="EUR")
        instructed.text = str(item.open_amount)
        if item.counterparty_bic:
            creditor_agent = ET.SubElement(transfer, "CdtrAgt")
            creditor_fin = ET.SubElement(creditor_agent, "FinInstnId")
            _text_element(creditor_fin, "BIC", item.counterparty_bic)
        creditor = ET.SubElement(transfer, "Cdtr")
        _text_element(creditor, "Nm", _sanitize(item.counterparty or item.reference, 70))
        creditor_account = ET.SubElement(transfer, "CdtrAcct")
        creditor_account_id = ET.SubElement(creditor_account, "Id")
        _text_element(creditor_account_id, "IBAN", item.counterparty_iban)
        remittance = ET.SubElement(transfer, "RmtInf")
        _text_element(remittance, "Ustrd", _sanitize(item.reference, 140))

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="payment_run",
        entity_id=message_id,
        action="created",
        changed_by=changed_by,
        payload={
            "open_item_ids": [item.id for item in items],
            "transaction_count": len(items),
            "control_sum": str(control_sum),
            "execution_date": execution_date.isoformat(),
        },
    )
    session.commit()

    return PaymentRunResult(
        xml_bytes=xml_bytes,
        file_name=f"zahllauf_{execution_date.isoformat()}_{message_id}.xml",
        transaction_count=len(items),
        control_sum=control_sum,
        open_item_ids=[item.id for item in items],
    )
