"""Konsolidierte Auswertung mehrerer Gesellschaften eines Mandanten (ADR-008).

Die Auswertung rechnet ausschließlich aus den Büchern der Mitgliedsgesellschaften; es
entstehen keine Buchungen in den Einzelbüchern. Zusammengefasst wird über die Kontonummer:

* **Summenbilanz:** je Konto und Gesellschaft der Saldo nach denselben Regeln wie die
  Einzelberichte (``reports``), dann Summe, Eliminierungen, manuelle Buchungen und
  konsolidierter Wert. Daraus entstehen konsolidierte SuSa, GuV und Bilanz.
* **Automatische Eliminierungen** werden nur berechnet, nie gebucht: Salden auf Sammelkonten
  und Erfolgszeilen von Buchungen mit einem Partner, der auf ein Mitglied zeigt
  (``BusinessPartner.intercompany_company_id``). Was zwischen zwei Gesellschaften nicht
  aufgeht, steht je Paar in der Eliminierungsliste und auf einer eigenen Zeile
  „Aufrechnungsdifferenz“: Die Bilanz geht auf, die Differenz bleibt sichtbar.
* **Manuelle Konsolidierungsbuchungen** (Kapitalkonsolidierung, Sonstiges) gehören der Gruppe
  und zählen nur in dieser Auswertung.

Kein Konzernabschluss nach §§ 290 ff. HGB; die Grenzen stehen in ``CONSOLIDATION_LIMITS``.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.services.account_chart_check import detect_company_chart
from app.services.audit_log import log_audit_event
from app.services.journal_entries import CARRYFORWARD_SOURCES, SOURCE_YEAR_END_CLOSE
from app.services.reports import (
    REVENUE_ACCOUNT_TYPES,
    account_balance_rows,
    balance_sheet_conditions,
    fiscal_year_for_date,
    income_statement_conditions,
    trial_balance_conditions,
)
from domain.formatting import format_amount
from domain.models import (
    CONSOLIDATION_ADJUSTMENT_KINDS,
    Account,
    BusinessPartner,
    Company,
    ConsolidationAdjustment,
    ConsolidationAdjustmentLine,
    ConsolidationGroup,
    ConsolidationMember,
    JournalEntry,
    JournalEntryLine,
    Period,
)

ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")

KIND_LABELS = {"capital": "Kapitalkonsolidierung", "other": "Sonstige"}
ACCOUNT_TYPE_LABELS = {
    "asset": "Aktivkonto",
    "liability": "Verbindlichkeit",
    "equity": "Eigenkapital",
    "income": "Erlöskonto",
    "revenue": "Erlöskonto",
    "expense": "Aufwandskonto",
}
PROFIT_AND_LOSS_TYPES = {*REVENUE_ACCOUNT_TYPES, "expense"}

# Zeilen ohne Konto: Aufrechnungsdifferenzen, Vortrag und Ergebnis.
DIFFERENCE_DEBT_CODE = "DIFF-S"
DIFFERENCE_DEBT_NAME = "Aufrechnungsdifferenz (Schulden)"
DIFFERENCE_INCOME_CODE = "DIFF-E"
DIFFERENCE_INCOME_NAME = "Aufrechnungsdifferenz (Aufwand/Ertrag)"
CARRY_CODE = "KONS-EV"
CARRY_NAME = "Ergebnisvortrag aus Konsolidierungsbuchungen"
NET_INCOME_CODE = "P&L"
NET_INCOME_NAME = "Jahresergebnis"

CONSOLIDATION_LIMITS = (
    "Konsolidierte Auswertung innerhalb eines Mandanten, kein Konzernabschluss nach "
    "§§ 290 ff. HGB und nicht geprüft.",
    "Keine Zwischenergebniseliminierung und keine latenten Steuern.",
    "Keine Equity-Methode und keine automatische Berechnung von Minderheitenanteilen.",
    "Keine Fremdwährungsumrechnung von Töchtern: Alle Mitglieder führen dieselbe Währung.",
    "Kein Konzernanhang.",
)

NAME_LIMIT = 120
DESCRIPTION_LIMIT = 255
MEMBER_FIELDS = {"company_id", "ownership_percent", "included_from", "included_to"}
GROUP_UPDATE_FIELDS = {"name", "parent_company_id", "is_active", "members"}


class ConsolidationError(ValueError):
    """Ungültige Konsolidierungsgruppe, Buchung oder Auswertung."""


# ---------------------------------------------------------------------------
# Darstellung
# ---------------------------------------------------------------------------


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def sorted_members(group: ConsolidationGroup) -> list[ConsolidationMember]:
    """Muttergesellschaft zuerst, danach nach Name."""
    return sorted(
        group.members,
        key=lambda member: (
            member.company_id != group.parent_company_id,
            member.company.name.lower(),
            member.company_id,
        ),
    )


def serialize_member(member: ConsolidationMember) -> dict[str, Any]:
    return {
        "company_id": member.company_id,
        "company_name": member.company.name,
        "ownership_percent": f"{member.ownership_percent:.2f}",
        "included_from": _iso(member.included_from),
        "included_to": _iso(member.included_to),
    }


def serialize_group(group: ConsolidationGroup) -> dict[str, Any]:
    return {
        "id": group.id,
        "tenant_id": group.tenant_id,
        "name": group.name,
        "parent_company_id": group.parent_company_id,
        "parent_company_name": group.parent_company.name,
        "is_active": group.is_active,
        "created_by": group.created_by,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "members": [serialize_member(member) for member in sorted_members(group)],
    }


def serialize_adjustment(adjustment: ConsolidationAdjustment) -> dict[str, Any]:
    return {
        "id": adjustment.id,
        "group_id": adjustment.group_id,
        "adjustment_date": adjustment.adjustment_date.isoformat(),
        "kind": adjustment.kind,
        "kind_label": KIND_LABELS.get(adjustment.kind, adjustment.kind),
        "description": adjustment.description,
        "created_by": adjustment.created_by,
        "created_at": adjustment.created_at.isoformat() if adjustment.created_at else None,
        "lines": [
            {
                "line_number": line.line_number,
                "account_code": line.account_code,
                "account_name": line.account_name,
                "account_type": line.account_type,
                "debit_amount": f"{line.debit_amount:.2f}",
                "credit_amount": f"{line.credit_amount:.2f}",
            }
            for line in adjustment.lines
        ],
    }


def json_ready(value: Any) -> Any:
    """Beträge und Daten eines Berichts maschinenlesbar für die API."""
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Eingaben prüfen
# ---------------------------------------------------------------------------


def _required_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ConsolidationError(f"{label} ist Pflicht.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ConsolidationError(f"{label} ist Pflicht.")
    if len(normalized) > limit:
        raise ConsolidationError(f"{label} darf höchstens {limit} Zeichen lang sein.")
    return normalized


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConsolidationError(f"{label} muss true oder false sein.")
    return value


def _optional_date(value: Any, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ConsolidationError(f"{label} muss ein Datum im Format JJJJ-MM-TT sein.") from exc


def _company_id(value: Any, label: str = "company_id") -> int:
    if isinstance(value, bool):
        raise ConsolidationError(f"{label} muss eine Gesellschafts-ID sein.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConsolidationError(f"{label} muss eine Gesellschafts-ID sein.") from exc


def _ownership_percent(value: Any) -> Decimal:
    if value is None or value == "":
        return HUNDRED
    try:
        percent = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ConsolidationError("Die Beteiligungsquote muss eine Zahl sein.") from exc
    if not percent.is_finite() or percent <= 0 or percent > HUNDRED:
        raise ConsolidationError("Die Beteiligungsquote muss über 0 und höchstens 100 % liegen.")
    quantized = percent.quantize(Decimal("0.01"))
    if quantized != percent:
        raise ConsolidationError("Die Beteiligungsquote hat höchstens zwei Nachkommastellen.")
    return quantized


def _amount(value: Any, label: str) -> Decimal:
    if value is None or value == "":
        return ZERO
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ConsolidationError(f"{label} ist keine gültige Zahl.") from exc
    if not amount.is_finite() or amount < 0:
        raise ConsolidationError(f"{label} darf nicht negativ sein.")
    quantized = amount.quantize(Decimal("0.01"))
    if quantized != amount:
        raise ConsolidationError(f"{label} hat mehr als zwei Nachkommastellen.")
    return quantized


def _member_company(session: Session, company_id: int, *, tenant_id: int) -> Company:
    company = session.get(Company, company_id)
    # Gleiche Meldung für fremd und unbekannt: Gesellschaften anderer Mandanten bleiben verborgen.
    if company is None or company.tenant_id != tenant_id:
        raise ConsolidationError(
            f"Gesellschaft {company_id} gehört nicht zum Mandanten der Gruppe."
        )
    return company


@dataclass(frozen=True, slots=True)
class _MemberInput:
    company: Company
    ownership_percent: Decimal
    included_from: date | None
    included_to: date | None


def _member_inputs(
    session: Session, raw_members: Any, *, tenant_id: int, currency_code: str
) -> list[_MemberInput]:
    if not isinstance(raw_members, (list, tuple)):
        raise ConsolidationError("members muss eine Liste sein.")
    inputs: dict[int, _MemberInput] = {}
    for raw in raw_members:
        if not isinstance(raw, dict):
            raise ConsolidationError("Jedes Mitglied braucht mindestens company_id.")
        unknown = set(raw) - MEMBER_FIELDS - {"company_name"}
        if unknown:
            raise ConsolidationError(
                f"Unbekannte Mitgliedsfelder: {', '.join(sorted(unknown))}."
            )
        company = _member_company(
            session, _company_id(raw.get("company_id")), tenant_id=tenant_id
        )
        if company.id in inputs:
            raise ConsolidationError(f"{company.name} ist doppelt aufgeführt.")
        if company.currency_code != currency_code:
            raise ConsolidationError(
                f"{company.name} führt {company.currency_code}, die Gruppe {currency_code}: "
                "Alle Mitglieder müssen dieselbe Währung nutzen."
            )
        included_from = _optional_date(raw.get("included_from"), "Einbezogen ab")
        included_to = _optional_date(raw.get("included_to"), "Einbezogen bis")
        if included_from and included_to and included_from > included_to:
            raise ConsolidationError(
                f"{company.name}: „Einbezogen ab“ liegt nach „Einbezogen bis“."
            )
        inputs[company.id] = _MemberInput(
            company=company,
            ownership_percent=_ownership_percent(raw.get("ownership_percent")),
            included_from=included_from,
            included_to=included_to,
        )
    return list(inputs.values())


def _require_parent_terms(inputs: list[_MemberInput], parent: Company) -> None:
    parent_input = next((item for item in inputs if item.company.id == parent.id), None)
    if parent_input is None:
        raise ConsolidationError("Die Muttergesellschaft muss Mitglied der Gruppe sein.")
    if parent_input.ownership_percent != HUNDRED:
        raise ConsolidationError("Die Muttergesellschaft wird mit 100 % geführt.")


def _sync_members(group: ConsolidationGroup, inputs: list[_MemberInput]) -> None:
    existing = {member.company_id: member for member in group.members}
    wanted = {item.company.id for item in inputs}
    for company_id, member in existing.items():
        if company_id not in wanted:
            group.members.remove(member)
    for item in inputs:
        member = existing.get(item.company.id)
        if member is None:
            member = ConsolidationMember(company=item.company, company_id=item.company.id)
            group.members.append(member)
        member.ownership_percent = item.ownership_percent
        member.included_from = item.included_from
        member.included_to = item.included_to


def _require_unique_name(
    session: Session, *, tenant_id: int, name: str, exclude_id: int | None = None
) -> None:
    stmt = select(ConsolidationGroup.id).where(
        ConsolidationGroup.tenant_id == tenant_id, ConsolidationGroup.name == name
    )
    if exclude_id is not None:
        stmt = stmt.where(ConsolidationGroup.id != exclude_id)
    if session.execute(stmt).first() is not None:
        raise ConsolidationError(f"Eine Gruppe „{name}“ gibt es in diesem Mandanten bereits.")


# ---------------------------------------------------------------------------
# Gruppen
# ---------------------------------------------------------------------------


def list_groups(
    *, session: Session, tenant_id: int | None = None, include_inactive: bool = True
) -> list[ConsolidationGroup]:
    stmt = select(ConsolidationGroup)
    if tenant_id is not None:
        stmt = stmt.where(ConsolidationGroup.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(ConsolidationGroup.is_active.is_(True))
    stmt = stmt.order_by(ConsolidationGroup.name, ConsolidationGroup.id)
    return list(session.execute(stmt).scalars().all())


def _audit(
    session: Session,
    *,
    group: ConsolidationGroup,
    entity_type: str,
    entity_id: int,
    action: str,
    changed_by: str,
    payload: dict[str, Any],
) -> None:
    log_audit_event(
        session=session,
        tenant_id=group.tenant_id,
        company_id=group.parent_company_id,
        entity_type=entity_type,
        entity_id=str(entity_id),
        action=action,
        changed_by=changed_by,
        payload=payload,
    )


def create_group(
    *,
    session: Session,
    parent_company: Company,
    changed_by: str,
    name: Any = None,
    members: Any = None,
    is_active: Any = True,
) -> ConsolidationGroup:
    """Legt eine Gruppe an; die Muttergesellschaft wird automatisch Mitglied."""
    group_name = _required_text(name, "Name", NAME_LIMIT)
    _require_unique_name(session, tenant_id=parent_company.tenant_id, name=group_name)
    inputs = _member_inputs(
        session,
        members or [],
        tenant_id=parent_company.tenant_id,
        currency_code=parent_company.currency_code,
    )
    if all(item.company.id != parent_company.id for item in inputs):
        inputs.insert(0, _MemberInput(parent_company, HUNDRED, None, None))
    _require_parent_terms(inputs, parent_company)

    group = ConsolidationGroup(
        tenant_id=parent_company.tenant_id,
        name=group_name,
        parent_company=parent_company,
        parent_company_id=parent_company.id,
        is_active=_bool(is_active, "is_active"),
        created_by=changed_by,
    )
    _sync_members(group, inputs)
    session.add(group)
    session.flush()
    _audit(
        session,
        group=group,
        entity_type="consolidation_group",
        entity_id=group.id,
        action="created",
        changed_by=changed_by,
        payload={"before": None, "after": serialize_group(group)},
    )
    return group


def _member_dict(member: ConsolidationMember) -> dict[str, Any]:
    return {
        "company_id": member.company_id,
        "ownership_percent": member.ownership_percent,
        "included_from": member.included_from,
        "included_to": member.included_to,
    }


def update_group(
    *, session: Session, group: ConsolidationGroup, changed_by: str, **changes: Any
) -> bool:
    """Ändert Name, Muttergesellschaft, Status oder die Mitgliederliste; False bei No-op.

    ``members`` ersetzt die ganze Liste. Beim Wechsel der Muttergesellschaft ohne Liste
    wird die neue Mutter ergänzt, falls sie noch nicht Mitglied ist.
    """
    unknown = set(changes) - GROUP_UPDATE_FIELDS
    if unknown:
        raise ConsolidationError(f"Nicht änderbare Felder: {', '.join(sorted(unknown))}.")
    if not changes:
        raise ConsolidationError("Mindestens ein änderbares Feld ist erforderlich.")
    before = serialize_group(group)

    parent = group.parent_company
    if "parent_company_id" in changes:
        parent = _member_company(
            session,
            _company_id(changes["parent_company_id"], "parent_company_id"),
            tenant_id=group.tenant_id,
        )
    if "name" in changes:
        group_name = _required_text(changes["name"], "Name", NAME_LIMIT)
        _require_unique_name(
            session, tenant_id=group.tenant_id, name=group_name, exclude_id=group.id
        )
        group.name = group_name
    if "is_active" in changes:
        group.is_active = _bool(changes["is_active"], "is_active")
    if "members" in changes or parent.id != group.parent_company_id:
        raw_members = changes.get("members", [_member_dict(m) for m in group.members])
        inputs = _member_inputs(
            session,
            raw_members,
            tenant_id=group.tenant_id,
            currency_code=parent.currency_code,
        )
        if "members" not in changes and all(item.company.id != parent.id for item in inputs):
            inputs.insert(0, _MemberInput(parent, HUNDRED, None, None))
        _require_parent_terms(inputs, parent)
        group.parent_company = parent
        group.parent_company_id = parent.id
        _sync_members(group, inputs)

    session.flush()
    after = serialize_group(group)
    if before == after:
        return False
    _audit(
        session,
        group=group,
        entity_type="consolidation_group",
        entity_id=group.id,
        action="updated",
        changed_by=changed_by,
        payload={"before": before, "after": after},
    )
    return True


def set_member(
    *,
    session: Session,
    group: ConsolidationGroup,
    changed_by: str,
    company_id: Any,
    ownership_percent: Any = None,
    included_from: Any = None,
    included_to: Any = None,
) -> bool:
    """Ergänzt ein Mitglied oder ändert dessen Quote und Einbeziehungszeitraum."""
    target = _company_id(company_id)
    raw_members = [_member_dict(m) for m in group.members if m.company_id != target]
    raw_members.append(
        {
            "company_id": target,
            "ownership_percent": ownership_percent,
            "included_from": included_from,
            "included_to": included_to,
        }
    )
    return update_group(session=session, group=group, changed_by=changed_by, members=raw_members)


def remove_member(
    *, session: Session, group: ConsolidationGroup, changed_by: str, company_id: Any
) -> bool:
    target = _company_id(company_id)
    raw_members = [_member_dict(m) for m in group.members if m.company_id != target]
    return update_group(session=session, group=group, changed_by=changed_by, members=raw_members)


# ---------------------------------------------------------------------------
# Kontenkatalog über alle Mitglieder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AccountInfo:
    name: str
    account_type: str | None
    # Kontoart je Gesellschaft, in der die Nummer vorkommt (für die Warnung).
    types: dict[str, str] = field(default_factory=dict)


def _account_catalog(
    session: Session, companies: Iterable[Company], adjustments: Iterable[ConsolidationAdjustment]
) -> dict[str, _AccountInfo]:
    """Kontonummern aller Gesellschaften; Name und Kontoart der ersten (Mutter zuerst)."""
    catalog: dict[str, _AccountInfo] = {}
    for company in companies:
        accounts = session.execute(
            select(Account.code, Account.name, Account.account_type).where(
                Account.company_id == company.id
            )
        ).all()
        for code, name, account_type in accounts:
            info = catalog.setdefault(code, _AccountInfo(name=name, account_type=account_type))
            info.types[company.name] = account_type
    # Konten aus Buchungen, deren Gesellschaft die Gruppe inzwischen verlassen hat.
    for adjustment in adjustments:
        for line in adjustment.lines:
            catalog.setdefault(
                line.account_code,
                _AccountInfo(name=line.account_name, account_type=line.account_type),
            )
    return catalog


def _catalog_entry(catalog: dict[str, _AccountInfo], code: str) -> _AccountInfo:
    return catalog.get(code) or _AccountInfo(name="Unbekanntes Konto", account_type=None)


def account_choices(*, session: Session, group: ConsolidationGroup) -> list[dict[str, str]]:
    """Kontonummern der Mitglieder für die Erfassung manueller Buchungen."""
    companies = [member.company for member in sorted_members(group)]
    catalog = _account_catalog(session, companies, [])
    return [
        {"code": code, "name": catalog[code].name, "account_type": catalog[code].account_type}
        for code in sorted(catalog)
    ]


# ---------------------------------------------------------------------------
# Manuelle Konsolidierungsbuchungen
# ---------------------------------------------------------------------------


def create_adjustment(
    *,
    session: Session,
    group: ConsolidationGroup,
    changed_by: str,
    adjustment_date: Any = None,
    kind: Any = None,
    description: Any = None,
    lines: Any = None,
) -> ConsolidationAdjustment:
    """Erfasst eine ausgeglichene Buchung auf Kontonummern der Mitglieder."""
    if not group.is_active:
        raise ConsolidationError("Die Gruppe ist inaktiv.")
    booked_on = _optional_date(adjustment_date, "Datum")
    if booked_on is None:
        raise ConsolidationError("Datum ist Pflicht.")
    if kind not in CONSOLIDATION_ADJUSTMENT_KINDS:
        raise ConsolidationError("Die Art muss capital oder other sein.")
    text = _required_text(description, "Text", DESCRIPTION_LIMIT)
    if not isinstance(lines, (list, tuple)) or len(lines) < 2:
        raise ConsolidationError("Eine Buchung braucht mindestens zwei Zeilen.")

    companies = [member.company for member in sorted_members(group)]
    catalog = _account_catalog(session, companies, [])
    adjustment_lines: list[ConsolidationAdjustmentLine] = []
    debit_total = credit_total = ZERO
    for number, raw in enumerate(lines, start=1):
        if not isinstance(raw, dict):
            raise ConsolidationError(f"Zeile {number}: Kontonummer, Soll und Haben fehlen.")
        code = str(raw.get("account_code") or "").strip()
        if not code:
            raise ConsolidationError(f"Zeile {number}: Die Kontonummer fehlt.")
        info = catalog.get(code)
        if info is None:
            raise ConsolidationError(
                f"Zeile {number}: Konto {code} gibt es in keiner Gesellschaft der Gruppe."
            )
        debit = _amount(raw.get("debit_amount"), f"Zeile {number}: Soll")
        credit = _amount(raw.get("credit_amount"), f"Zeile {number}: Haben")
        if (debit > ZERO) == (credit > ZERO):
            raise ConsolidationError(
                f"Zeile {number}: Es muss genau einer der Beträge Soll oder Haben gesetzt sein."
            )
        debit_total += debit
        credit_total += credit
        adjustment_lines.append(
            ConsolidationAdjustmentLine(
                line_number=number,
                account_code=code,
                account_name=info.name[:255],
                account_type=info.account_type,
                debit_amount=debit,
                credit_amount=credit,
            )
        )
    if debit_total != credit_total:
        raise ConsolidationError(
            f"Die Buchung ist nicht ausgeglichen: Soll {format_amount(debit_total)}, "
            f"Haben {format_amount(credit_total)}."
        )
    adjustment = ConsolidationAdjustment(
        tenant_id=group.tenant_id,
        group=group,
        adjustment_date=booked_on,
        kind=kind,
        description=text,
        created_by=changed_by,
        lines=adjustment_lines,
    )
    session.add(adjustment)
    session.flush()
    _audit(
        session,
        group=group,
        entity_type="consolidation_adjustment",
        entity_id=adjustment.id,
        action="created",
        changed_by=changed_by,
        payload={"before": None, "after": serialize_adjustment(adjustment)},
    )
    return adjustment


def delete_adjustment(
    *,
    session: Session,
    adjustment: ConsolidationAdjustment,
    changed_by: str,
    reason: Any = None,
) -> None:
    """Löscht eine Buchung; der vollständige Inhalt bleibt im Audit-Log (ADR-008)."""
    group = adjustment.group
    before = serialize_adjustment(adjustment)
    adjustment_id = adjustment.id
    session.delete(adjustment)
    session.flush()
    session.expire(group, ["adjustments"])
    note = reason.strip() if isinstance(reason, str) else None
    _audit(
        session,
        group=group,
        entity_type="consolidation_adjustment",
        entity_id=adjustment_id,
        action="deleted",
        changed_by=changed_by,
        payload={"before": before, "after": None, "reason": note or None},
    )


# ---------------------------------------------------------------------------
# Bericht: Mitglieder und Zeiträume
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Member:
    company: Company
    ownership_percent: Decimal
    included_from: date | None
    included_to: date | None
    is_parent: bool

    @property
    def id(self) -> int:
        return self.company.id

    @property
    def name(self) -> str:
        return self.company.name

    def covers(self, day: date) -> bool:
        return (self.included_from is None or self.included_from <= day) and (
            self.included_to is None or day <= self.included_to
        )

    def window(
        self, date_from: date | None, date_to: date | None
    ) -> tuple[date | None, date | None] | None:
        """Schnitt aus Auswertungszeitraum und Einbeziehung; None ohne Überschneidung."""
        start = max(filter(None, (date_from, self.included_from)), default=None)
        end = min(filter(None, (date_to, self.included_to)), default=None)
        if start is not None and end is not None and start > end:
            return None
        return start, end


def _report_members(group: ConsolidationGroup) -> list[_Member]:
    """Mitglieder für den Bericht; Mandant und Währung werden erneut geprüft."""
    parent = group.parent_company
    members: list[_Member] = []
    for member in sorted_members(group):
        company = member.company
        if company.tenant_id != group.tenant_id:
            raise ConsolidationError(f"{company.name} gehört nicht zum Mandanten der Gruppe.")
        if company.currency_code != parent.currency_code:
            raise ConsolidationError(
                f"{company.name} führt {company.currency_code}, die Gruppe "
                f"{parent.currency_code}: Eine Umrechnung gibt es nicht."
            )
        members.append(
            _Member(
                company=company,
                ownership_percent=member.ownership_percent,
                included_from=member.included_from,
                included_to=member.included_to,
                is_parent=member.company_id == group.parent_company_id,
            )
        )
    return members


# ---------------------------------------------------------------------------
# Bericht: Eliminierungen
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Pair:
    first: _Member
    second: _Member
    balances: dict[int, Decimal]
    # (Gesellschaft, Konto, Bezeichnung, Partner) -> Saldo
    lines: dict[tuple[int, str, str, str], Decimal] = field(default_factory=dict)


class _Eliminations:
    """Sammelt zu eliminierende Salden (Soll − Haben) je Paar von Gesellschaften."""

    def __init__(self) -> None:
        self._pairs: dict[tuple[int, int], _Pair] = {}

    def add(
        self,
        owner: _Member,
        counterparty: _Member,
        *,
        code: str,
        account_name: str,
        partner_name: str,
        balance: Decimal,
    ) -> None:
        first, second = sorted((owner, counterparty), key=lambda member: member.id)
        pair = self._pairs.setdefault(
            (first.id, second.id),
            _Pair(first=first, second=second, balances={first.id: ZERO, second.id: ZERO}),
        )
        pair.balances[owner.id] += balance
        key = (owner.id, code, account_name, partner_name)
        pair.lines[key] = pair.lines.get(key, ZERO) + balance

    def by_code(self) -> dict[str, Decimal]:
        """Wirkung je Kontonummer: Der Saldo wird aus dem Konto herausgenommen."""
        effects: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for pair in self._pairs.values():
            for (_, code, _, _), balance in pair.lines.items():
                effects[code] -= balance
        return dict(effects)

    def difference(self) -> Decimal:
        """Was nach der Aufrechnung übrig bleibt (Soll − Haben)."""
        return sum(
            (balance for pair in self._pairs.values() for balance in pair.balances.values()),
            ZERO,
        )

    def pairs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        names: dict[int, str] = {}
        for pair in self._pairs.values():
            names[pair.first.id] = pair.first.name
            names[pair.second.id] = pair.second.name
        for key in sorted(self._pairs):
            pair = self._pairs[key]
            if not any(pair.lines.values()):
                continue
            balance_first = pair.balances[pair.first.id]
            balance_second = pair.balances[pair.second.id]
            rows.append(
                {
                    "company_a_id": pair.first.id,
                    "company_a_name": pair.first.name,
                    "company_b_id": pair.second.id,
                    "company_b_name": pair.second.name,
                    "balance_a": balance_first,
                    "balance_b": balance_second,
                    "difference": balance_first + balance_second,
                    "is_matched": balance_first + balance_second == ZERO,
                    "lines": [
                        {
                            "company_id": company_id,
                            "company_name": names[company_id],
                            "code": code,
                            "name": account_name,
                            "partner_name": partner_name,
                            "balance": balance,
                        }
                        for (company_id, code, account_name, partner_name), balance in sorted(
                            pair.lines.items()
                        )
                        if balance != ZERO
                    ],
                }
            )
        return rows

    def as_report(self) -> dict[str, Any]:
        return {"pairs": self.pairs(), "difference": self.difference()}


@dataclass(slots=True)
class _ScanNotes:
    """Auffälligkeiten beim Durchsuchen der Buchungen mit verbundenen Partnern."""

    # (Gesellschaft, Buchungsnummer) mit Erfolgszeile ohne eindeutigen Partner.
    ambiguous: set[tuple[str, str]] = field(default_factory=set)
    # (Gesellschaft, Partner) -> Saldo gegenüber Gesellschaften außerhalb des Kreises.
    outside: dict[tuple[str, str], Decimal] = field(default_factory=dict)


def _intercompany_entry_lines(
    session: Session,
    company_id: int,
    *,
    date_from: date | None,
    date_to: date | None,
    conditions: list,
) -> dict[int, list[Any]]:
    """Zeilen aller Buchungen, in denen mindestens eine Zeile einen verbundenen Partner trägt."""
    marked_line = aliased(JournalEntryLine)
    marked_partner = aliased(BusinessPartner)
    marked_entries = (
        select(marked_line.journal_entry_id)
        .join(marked_partner, marked_partner.id == marked_line.partner_id)
        .where(marked_partner.intercompany_company_id.is_not(None))
    )
    stmt = (
        select(
            JournalEntry.id.label("entry_id"),
            JournalEntry.posting_number,
            JournalEntry.entry_date,
            JournalEntry.source,
            JournalEntry.fiscal_year_id,
            JournalEntryLine.debit_amount,
            JournalEntryLine.credit_amount,
            JournalEntryLine.partner_id,
            Account.code,
            Account.name.label("account_name"),
            Account.account_type,
            Account.subledger,
            BusinessPartner.name.label("partner_name"),
            BusinessPartner.intercompany_company_id.label("counterparty_id"),
        )
        .join(JournalEntryLine, JournalEntryLine.journal_entry_id == JournalEntry.id)
        .join(Account, Account.id == JournalEntryLine.account_id)
        .join(Period, Period.id == JournalEntry.period_id)
        .outerjoin(BusinessPartner, BusinessPartner.id == JournalEntryLine.partner_id)
        .where(JournalEntry.company_id == company_id, JournalEntry.id.in_(marked_entries))
        .order_by(JournalEntry.id, JournalEntryLine.line_number)
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    for condition in conditions:
        stmt = stmt.where(condition)
    entries: dict[int, list[Any]] = defaultdict(list)
    for row in session.execute(stmt).all():
        entries[row.entry_id].append(row)
    return entries


def _income_counterparty(line: Any, entry_lines: list[Any]) -> tuple[int | None, bool]:
    """Gegenseite einer Erfolgszeile: (Gesellschaft, mehrdeutig).

    Der eigene Partner der Zeile entscheidet. Ohne eigenen Partner gilt der Partner der
    Buchung, wenn er eindeutig ist; bei mehreren oder gemischten Partnern lässt sich die
    Zeile nicht zuordnen und bleibt stehen.
    """
    if line.partner_id is not None:
        return line.counterparty_id, False
    counterparties = {row.counterparty_id for row in entry_lines if row.partner_id is not None}
    marked = counterparties - {None}
    if len(marked) == 1 and None not in counterparties:
        return next(iter(marked)), False
    return None, bool(marked)


def _scan_intercompany(
    session: Session,
    member: _Member,
    members_by_id: dict[int, _Member],
    *,
    date_from: date | None,
    date_to: date | None,
    conditions: list,
    in_scope: Callable[[_Member, date], bool],
    debt: _Eliminations | None = None,
    income: _Eliminations | None = None,
    closed_years: frozenset[int] = frozenset(),
    notes: _ScanNotes,
    report_outside: bool = False,
) -> None:
    entries = _intercompany_entry_lines(
        session, member.id, date_from=date_from, date_to=date_to, conditions=conditions
    )
    for entry_lines in entries.values():
        for line in entry_lines:
            balance = line.debit_amount - line.credit_amount
            if debt is not None and line.subledger and line.counterparty_id is not None:
                counterparty = members_by_id.get(line.counterparty_id)
                if (
                    counterparty is not None
                    and counterparty.id != member.id
                    and in_scope(counterparty, line.entry_date)
                ):
                    debt.add(
                        member,
                        counterparty,
                        code=line.code,
                        account_name=line.account_name,
                        partner_name=line.partner_name,
                        balance=balance,
                    )
                elif report_outside:
                    key = (member.name, line.partner_name)
                    notes.outside[key] = notes.outside.get(key, ZERO) + balance
            if (
                income is None
                or line.account_type not in PROFIT_AND_LOSS_TYPES
                or line.source in CARRYFORWARD_SOURCES
                or line.fiscal_year_id in closed_years
            ):
                continue
            counterparty_id, ambiguous = _income_counterparty(line, entry_lines)
            if ambiguous:
                if any(row.counterparty_id in members_by_id for row in entry_lines):
                    notes.ambiguous.add((member.name, line.posting_number))
                continue
            counterparty = members_by_id.get(counterparty_id) if counterparty_id else None
            if (
                counterparty is not None
                and counterparty.id != member.id
                and in_scope(counterparty, line.entry_date)
            ):
                income.add(
                    member,
                    counterparty,
                    code=line.code,
                    account_name=line.account_name,
                    partner_name=line.partner_name or "Partner der Buchung",
                    balance=balance,
                )


def _closed_years(
    session: Session, company_id: int, date_from: date | None, date_to: date | None
) -> frozenset[int]:
    """Geschäftsjahre, deren Ergebnisvortrag in der SuSa mitzählt: Dort sind die
    Erfolgskonten glattgestellt, eine Eliminierung ginge ins Leere."""
    stmt = select(JournalEntry.fiscal_year_id).where(
        JournalEntry.company_id == company_id, JournalEntry.source == SOURCE_YEAR_END_CLOSE
    )
    if date_from is not None:
        stmt = stmt.where(JournalEntry.entry_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(JournalEntry.entry_date <= date_to)
    return frozenset(session.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Bericht: Zeilen
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    code: str
    name: str
    account_type: str | None
    balances: dict[int, Decimal] = field(default_factory=dict)
    debit_total: Decimal = ZERO
    credit_total: Decimal = ZERO
    eliminations: Decimal = ZERO
    adjustments: Decimal = ZERO


def _fold_rows(
    rows_by_company: dict[int, list[dict[str, Any]]],
    *,
    eliminations: dict[str, Decimal],
    adjustments: dict[str, Decimal],
    catalog: dict[str, _AccountInfo],
) -> dict[str, _Row]:
    """Fasst die Salden aller Gesellschaften über die Kontonummer zusammen."""
    rows: dict[str, _Row] = {}

    def row_for(code: str) -> _Row:
        if code not in rows:
            info = _catalog_entry(catalog, code)
            rows[code] = _Row(code=code, name=info.name, account_type=info.account_type)
        return rows[code]

    for company_id, company_rows in rows_by_company.items():
        for source in company_rows:
            row = row_for(source["code"])
            balance = source["debit_total"] - source["credit_total"]
            row.balances[company_id] = row.balances.get(company_id, ZERO) + balance
            row.debit_total += source["debit_total"]
            row.credit_total += source["credit_total"]
    for code, amount in eliminations.items():
        row_for(code).eliminations += amount
    for code, amount in adjustments.items():
        row_for(code).adjustments += amount
    return rows


def _row_out(row: _Row, *, debit_side: bool, member_ids: list[int]) -> dict[str, Any]:
    """Berichtszeile; ``debit_side=False`` dreht das Vorzeichen (Passiva, Erträge)."""

    def signed(value: Decimal) -> Decimal:
        return value if debit_side else ZERO - value

    amounts = {company_id: signed(row.balances.get(company_id, ZERO)) for company_id in member_ids}
    total = sum(amounts.values(), ZERO)
    eliminations = signed(row.eliminations)
    adjustments = signed(row.adjustments)
    return {
        "code": row.code,
        "name": row.name,
        "account_type": row.account_type,
        "is_difference": False,
        "amounts": amounts,
        "total": total,
        "eliminations": eliminations,
        "adjustments": adjustments,
        "amount": total + eliminations + adjustments,
    }


def _difference_row(code: str, name: str, amount: Decimal, member_ids: list[int]) -> dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "account_type": None,
        "is_difference": True,
        "amounts": {company_id: ZERO for company_id in member_ids},
        "total": ZERO,
        "eliminations": amount,
        "adjustments": ZERO,
        "amount": amount,
    }


def _summary(rows: list[dict[str, Any]], member_ids: list[int]) -> dict[str, Any]:
    """Spaltensummen einer Zeilenliste."""
    return {
        "amounts": {
            company_id: sum((row["amounts"][company_id] for row in rows), ZERO)
            for company_id in member_ids
        },
        "total": sum((row["total"] for row in rows), ZERO),
        "eliminations": sum((row["eliminations"] for row in rows), ZERO),
        "adjustments": sum((row["adjustments"] for row in rows), ZERO),
        "amount": sum((row["amount"] for row in rows), ZERO),
    }


def _adjustment_effects(
    adjustments: Iterable[ConsolidationAdjustment],
    *,
    boundary: date | None,
    date_to: date | None,
    carry: bool,
    catalog: dict[str, _AccountInfo],
) -> tuple[dict[str, Decimal], Decimal]:
    """Wirkung der manuellen Buchungen je Kontonummer (Soll − Haben).

    Buchungen vor ``boundary`` wirken mit ``carry`` weiter wie ein Saldovortrag: Bestandskonten
    behalten sie, Erfolgskonten laufen in den Ergebnisvortrag (zweiter Rückgabewert). Sonst
    zählen sie nicht.
    """
    effects: dict[str, Decimal] = defaultdict(lambda: ZERO)
    carried = ZERO
    for adjustment in adjustments:
        if date_to is not None and adjustment.adjustment_date > date_to:
            continue
        current = boundary is None or adjustment.adjustment_date >= boundary
        if not current and not carry:
            continue
        for line in adjustment.lines:
            balance = line.debit_amount - line.credit_amount
            account_type = _catalog_entry(catalog, line.account_code).account_type
            if not current and account_type in PROFIT_AND_LOSS_TYPES:
                carried += balance
            else:
                effects[line.account_code] += balance
    return dict(effects), carried


def _balance_sheet(
    rows: dict[str, _Row],
    *,
    carried: Decimal,
    debt: _Eliminations,
    member_ids: list[int],
    as_of: date | None,
    fiscal_year_label: str | None,
    include_closing_entries: bool,
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    liabilities_and_equity: list[dict[str, Any]] = []
    net_income = _Row(code=NET_INCOME_CODE, name=NET_INCOME_NAME, account_type="equity")
    for code in sorted(rows):
        row = rows[code]
        if row.account_type == "asset":
            assets.append(_row_out(row, debit_side=True, member_ids=member_ids))
        elif row.account_type in {"liability", "equity"}:
            liabilities_and_equity.append(_row_out(row, debit_side=False, member_ids=member_ids))
        elif row.account_type in PROFIT_AND_LOSS_TYPES:
            for company_id, balance in row.balances.items():
                net_income.balances[company_id] = (
                    net_income.balances.get(company_id, ZERO) + balance
                )
            net_income.adjustments += row.adjustments
    if carried != ZERO:
        carry_row = _Row(
            code=CARRY_CODE, name=CARRY_NAME, account_type="equity", adjustments=carried
        )
        liabilities_and_equity.append(_row_out(carry_row, debit_side=False, member_ids=member_ids))
    result_row = _row_out(net_income, debit_side=False, member_ids=member_ids)
    if any(result_row["amounts"].values()) or result_row["adjustments"] != ZERO:
        liabilities_and_equity.append(result_row)
    difference = debt.difference()
    if difference > ZERO:
        assets.append(
            _difference_row(DIFFERENCE_DEBT_CODE, DIFFERENCE_DEBT_NAME, difference, member_ids)
        )
    elif difference < ZERO:
        liabilities_and_equity.append(
            _difference_row(
                DIFFERENCE_DEBT_CODE, DIFFERENCE_DEBT_NAME, ZERO - difference, member_ids
            )
        )
    summary_assets = _summary(assets, member_ids)
    summary_liabilities = _summary(liabilities_and_equity, member_ids)
    total_assets = summary_assets["amount"]
    total_liabilities = summary_liabilities["amount"]
    return {
        "period": {
            "as_of": _iso(as_of),
            "fiscal_year": fiscal_year_label,
            "include_closing_entries": include_closing_entries,
        },
        "assets": assets,
        "liabilities_and_equity": liabilities_and_equity,
        "summary": {"assets": summary_assets, "liabilities_and_equity": summary_liabilities},
        "totals": {
            "total_assets": total_assets,
            "total_liabilities_and_equity": total_liabilities,
            "difference": total_assets - total_liabilities,
            "is_balanced": total_assets == total_liabilities,
            "intercompany_difference": difference,
        },
    }


def _income_statement(
    rows: dict[str, _Row],
    *,
    income: _Eliminations,
    member_ids: list[int],
    date_from: date | None,
    date_to: date | None,
    include_closing_entries: bool,
) -> dict[str, Any]:
    revenues: list[dict[str, Any]] = []
    expenses: list[dict[str, Any]] = []
    for code in sorted(rows):
        row = rows[code]
        if row.account_type in REVENUE_ACCOUNT_TYPES:
            revenues.append(_row_out(row, debit_side=False, member_ids=member_ids))
        elif row.account_type == "expense":
            expenses.append(_row_out(row, debit_side=True, member_ids=member_ids))
    # Eine Aufwands- oder Ertragszeile, die nicht aufgeht, verändert das Ergebnis nicht:
    # Die Differenz steht auf eigener Zeile, das Ergebnis bleibt die Summe der Gesellschaften.
    difference = income.difference()
    if difference < ZERO:
        revenues.append(
            _difference_row(
                DIFFERENCE_INCOME_CODE, DIFFERENCE_INCOME_NAME, ZERO - difference, member_ids
            )
        )
    elif difference > ZERO:
        expenses.append(
            _difference_row(DIFFERENCE_INCOME_CODE, DIFFERENCE_INCOME_NAME, difference, member_ids)
        )
    summary_revenues = _summary(revenues, member_ids)
    summary_expenses = _summary(expenses, member_ids)
    net_income = {
        "amounts": {
            company_id: summary_revenues["amounts"][company_id]
            - summary_expenses["amounts"][company_id]
            for company_id in member_ids
        },
        **{
            key: summary_revenues[key] - summary_expenses[key]
            for key in ("total", "eliminations", "adjustments", "amount")
        },
    }
    return {
        "period": {
            "date_from": _iso(date_from),
            "date_to": _iso(date_to),
            "include_closing_entries": include_closing_entries,
        },
        "revenues": revenues,
        "expenses": expenses,
        "summary": {
            "revenues": summary_revenues,
            "expenses": summary_expenses,
            "net_income": net_income,
        },
        "totals": {
            "total_revenue": summary_revenues["amount"],
            "total_expense": summary_expenses["amount"],
            "net_income": net_income["amount"],
            "intercompany_difference": difference,
        },
    }


def _trial_balance(
    rows: dict[str, _Row],
    *,
    carried: Decimal,
    debt: _Eliminations,
    income: _Eliminations,
    member_ids: list[int],
) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for code in sorted(rows):
        row = rows[code]
        out.append(
            {
                **_row_out(row, debit_side=True, member_ids=member_ids),
                "debit_total": row.debit_total,
                "credit_total": row.credit_total,
            }
        )
    if carried != ZERO:
        carry_row = _Row(
            code=CARRY_CODE, name=CARRY_NAME, account_type="equity", adjustments=carried
        )
        out.append(
            {
                **_row_out(carry_row, debit_side=True, member_ids=member_ids),
                "debit_total": ZERO,
                "credit_total": ZERO,
            }
        )
    for code, name, amount in (
        (DIFFERENCE_DEBT_CODE, DIFFERENCE_DEBT_NAME, debt.difference()),
        (DIFFERENCE_INCOME_CODE, DIFFERENCE_INCOME_NAME, income.difference()),
    ):
        if amount != ZERO:
            out.append(
                {
                    **_difference_row(code, name, amount, member_ids),
                    "debit_total": ZERO,
                    "credit_total": ZERO,
                }
            )
    summary = _summary(out, member_ids)
    return {
        "rows": out,
        "summary": summary,
        "totals": {
            "debit_total": sum((row["debit_total"] for row in out), ZERO),
            "credit_total": sum((row["credit_total"] for row in out), ZERO),
            "balance": summary["amount"],
            "is_balanced": summary["amount"] == ZERO,
            "intercompany_difference": {
                "debt": debt.difference(),
                "income_expense": income.difference(),
            },
        },
    }


# ---------------------------------------------------------------------------
# Bericht: Hinweise
# ---------------------------------------------------------------------------


def _chart_warnings(session: Session, members: list[_Member]) -> list[str]:
    charts: dict[str, str] = {}
    for member in members:
        chart = detect_company_chart(session=session, company_id=member.id)
        if chart is not None:
            charts[member.name] = chart.upper()
    if len(set(charts.values())) <= 1:
        return []
    listing = ", ".join(f"{name}: {chart}" for name, chart in charts.items())
    return [
        f"Unterschiedliche Kontenrahmen ({listing}). Die Zusammenfassung läuft über die "
        "Kontonummer; gleiche Nummern können verschiedene Konten bezeichnen."
    ]


def _account_type_warnings(
    codes: Iterable[str], catalog: dict[str, _AccountInfo]
) -> list[str]:
    warnings: list[str] = []
    for code in sorted(set(codes)):
        info = catalog.get(code)
        if info is None or len({ACCOUNT_TYPE_LABELS.get(t, t) for t in info.types.values()}) <= 1:
            continue
        listing = ", ".join(
            f"{name}: {ACCOUNT_TYPE_LABELS.get(account_type, account_type)}"
            for name, account_type in info.types.items()
        )
        used = ACCOUNT_TYPE_LABELS.get(info.account_type, info.account_type)
        warnings.append(
            f"Konto {code} hat unterschiedliche Kontoarten ({listing}). Die Zusammenfassung "
            f"läuft über die Kontonummer und wertet die Zeile als {used}."
        )
    return warnings


def _date_de(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _inclusion_label(member: _Member) -> str:
    parts = []
    if member.included_from:
        parts.append(f"ab {_date_de(member.included_from)}")
    if member.included_to:
        parts.append(f"bis {_date_de(member.included_to)}")
    return " ".join(parts)


def _member_notes(members: list[_Member], as_of: date) -> list[str]:
    notes: list[str] = []
    for member in members:
        if not member.is_parent and member.ownership_percent < HUNDRED:
            notes.append(
                f"{member.name}: Beteiligungsquote {format_amount(member.ownership_percent)} %, "
                f"rechnerischer Fremdanteil {format_amount(HUNDRED - member.ownership_percent)} %. "
                "Minderheitenanteile berechnet die Auswertung nicht; sie sind bei Bedarf als "
                "manuelle Konsolidierungsbuchung zu erfassen."
            )
        if member.included_from or member.included_to:
            notes.append(
                f"{member.name}: einbezogen {_inclusion_label(member)}. "
                "SuSa und GuV zählen nur Buchungen in diesem Zeitraum, die Bilanz den vollen "
                "Bestand zum Stichtag einschließlich des Ergebnisses vor der Einbeziehung."
            )
        if not member.covers(as_of):
            notes.append(
                f"{member.name} ist zum Stichtag {_date_de(as_of)} nicht im "
                "Konsolidierungskreis und fehlt in der Bilanz."
            )
    return notes


def _scan_warnings(notes: _ScanNotes) -> list[str]:
    warnings = [
        f"{company}, Buchung {posting_number}: Erfolgszeile ohne eigenen Partner in einer "
        "Buchung mit mehreren Partnern; sie wurde nicht eliminiert. Bitte den Partner an der "
        "Zeile setzen oder manuell konsolidieren."
        for company, posting_number in sorted(notes.ambiguous)
    ]
    for (company, partner), balance in sorted(notes.outside.items()):
        if balance != ZERO:
            warnings.append(
                f"{company} führt gegenüber {partner} einen Saldo von {format_amount(balance)} "
                "(Soll − Haben). Die verbundene Gesellschaft ist zum Stichtag nicht im "
                "Konsolidierungskreis; der Saldo bleibt stehen."
            )
    return warnings


def _difference_warnings(label: str, eliminations: _Eliminations) -> list[str]:
    return [
        f"Aufrechnungsdifferenz {label} {pair['company_a_name']} / {pair['company_b_name']}: "
        f"{format_amount(pair['balance_a'])} gegen "
        f"{format_amount(pair['balance_b'])}, Differenz {format_amount(pair['difference'])}."
        for pair in eliminations.pairs()
        if not pair["is_matched"]
    ]


# ---------------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------------


def consolidation_report(
    *,
    session: Session,
    group: ConsolidationGroup,
    date_from: date | None = None,
    date_to: date | None = None,
    include_closing_entries: bool = False,
) -> dict[str, Any]:
    """Konsolidierte SuSa, GuV und Bilanz einer Gruppe mit Spalten je Gesellschaft.

    SuSa und GuV gelten für den Zeitraum, die Bilanz für den Stichtag ``date_to`` (ohne Angabe:
    heute für das Geschäftsjahr, alle Buchungen). Jede Gesellschaft folgt denselben Regeln wie
    ihre Einzelberichte; die Einbeziehung ab/bis begrenzt den Zeitraum und entscheidet, ob sie
    zum Stichtag in der Bilanz steht.
    """
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ConsolidationError("Das Von-Datum liegt nach dem Bis-Datum.")
    members = _report_members(group)
    members_by_id = {member.id: member for member in members}
    member_ids = list(members_by_id)
    parent = next(member for member in members if member.is_parent)
    adjustments = sorted(
        group.adjustments, key=lambda adjustment: (adjustment.adjustment_date, adjustment.id)
    )
    catalog = _account_catalog(session, [member.company for member in members], adjustments)
    notes = _ScanNotes()
    as_of = date_to or date.today()

    # Bilanz zum Stichtag: Bestand aller Gesellschaften, die dann einbezogen sind.
    stock_members = {member.id for member in members if member.covers(as_of)}
    stock_rows: dict[int, list[dict[str, Any]]] = {}
    debt = _Eliminations()
    for member in members:
        if member.id not in stock_members:
            continue
        conditions, _ = balance_sheet_conditions(
            session=session,
            company_id=member.id,
            as_of=as_of,
            include_closing_entries=include_closing_entries,
        )
        stock_rows[member.id] = account_balance_rows(
            session, member.id, date_from=None, date_to=date_to, conditions=conditions
        )
        _scan_intercompany(
            session,
            member,
            members_by_id,
            date_from=None,
            date_to=date_to,
            conditions=conditions,
            in_scope=lambda counterparty, _day: counterparty.id in stock_members,
            debt=debt,
            notes=notes,
            report_outside=True,
        )
    parent_year = fiscal_year_for_date(session, parent.id, as_of)
    stock_adjustments, stock_carried = _adjustment_effects(
        adjustments,
        boundary=parent_year.start_date if parent_year else None,
        date_to=date_to,
        carry=True,
        catalog=catalog,
    )
    stock_folded = _fold_rows(
        stock_rows, eliminations=debt.by_code(), adjustments=stock_adjustments, catalog=catalog
    )
    balance_sheet = _balance_sheet(
        stock_folded,
        carried=stock_carried,
        debt=debt,
        member_ids=member_ids,
        as_of=date_to,
        fiscal_year_label=parent_year.label if parent_year else None,
        include_closing_entries=include_closing_entries,
    )

    # GuV und SuSa für den Zeitraum: Nur Buchungen, solange beide Seiten einbezogen sind.
    def in_window(counterparty: _Member, day: date) -> bool:
        return counterparty.covers(day)

    income_rows: dict[int, list[dict[str, Any]]] = {}
    trial_rows: dict[int, list[dict[str, Any]]] = {}
    income = _Eliminations()
    trial_debt = _Eliminations()
    trial_income = _Eliminations()
    for member in members:
        window = member.window(date_from, date_to)
        if window is None:
            continue
        start, end = window
        conditions = income_statement_conditions(include_closing_entries=include_closing_entries)
        income_rows[member.id] = account_balance_rows(
            session, member.id, date_from=start, date_to=end, conditions=conditions
        )
        _scan_intercompany(
            session,
            member,
            members_by_id,
            date_from=start,
            date_to=end,
            conditions=conditions,
            in_scope=in_window,
            income=income,
            notes=notes,
        )
        conditions = trial_balance_conditions(
            date_from=start, include_closing_entries=include_closing_entries
        )
        trial_rows[member.id] = account_balance_rows(
            session, member.id, date_from=start, date_to=end, conditions=conditions
        )
        _scan_intercompany(
            session,
            member,
            members_by_id,
            date_from=start,
            date_to=end,
            conditions=conditions,
            in_scope=in_window,
            debt=trial_debt,
            income=trial_income,
            closed_years=(
                _closed_years(session, member.id, start, end)
                if include_closing_entries
                else frozenset()
            ),
            notes=notes,
        )
    income_adjustments, _ = _adjustment_effects(
        adjustments, boundary=date_from, date_to=date_to, carry=False, catalog=catalog
    )
    income_folded = _fold_rows(
        income_rows,
        eliminations=income.by_code(),
        adjustments=income_adjustments,
        catalog=catalog,
    )
    income_statement = _income_statement(
        income_folded,
        income=income,
        member_ids=member_ids,
        date_from=date_from,
        date_to=date_to,
        include_closing_entries=include_closing_entries,
    )
    trial_adjustments, trial_carried = _adjustment_effects(
        adjustments, boundary=date_from, date_to=date_to, carry=True, catalog=catalog
    )
    trial_eliminations: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for source in (trial_debt.by_code(), trial_income.by_code()):
        for code, amount in source.items():
            trial_eliminations[code] += amount
    trial_folded = _fold_rows(
        trial_rows,
        eliminations=trial_eliminations,
        adjustments=trial_adjustments,
        catalog=catalog,
    )
    trial_balance = _trial_balance(
        trial_folded,
        carried=trial_carried,
        debt=trial_debt,
        income=trial_income,
        member_ids=member_ids,
    )

    warnings = [
        *_chart_warnings(session, members),
        *_account_type_warnings(
            [*stock_folded, *income_folded, *trial_folded], catalog
        ),
        *_difference_warnings("Schulden", debt),
        *_difference_warnings("Aufwand/Ertrag", income),
        *_scan_warnings(notes),
    ]
    return {
        "group": serialize_group(group),
        "currency_code": parent.company.currency_code,
        "period": {
            "date_from": _iso(date_from),
            "date_to": _iso(date_to),
            "as_of": as_of.isoformat(),
            "include_closing_entries": include_closing_entries,
        },
        "companies": [
            {
                "company_id": member.id,
                "name": member.name,
                "is_parent": member.is_parent,
                "ownership_percent": member.ownership_percent,
                "minority_percent": (
                    ZERO if member.is_parent else HUNDRED - member.ownership_percent
                ),
                "included_from": member.included_from,
                "included_to": member.included_to,
                "in_balance_sheet": member.id in stock_members,
                "in_period": member.window(date_from, date_to) is not None,
            }
            for member in members
        ],
        "trial_balance": trial_balance,
        "income_statement": income_statement,
        "balance_sheet": balance_sheet,
        "eliminations": {"debt": debt.as_report(), "income_expense": income.as_report()},
        "adjustments": [serialize_adjustment(adjustment) for adjustment in adjustments],
        "warnings": warnings,
        "notes": _member_notes(members, as_of),
        "limits": list(CONSOLIDATION_LIMITS),
    }


def consolidation_report_csv(report: dict[str, Any]) -> str:
    """Bericht als CSV: eine Zeile je Konto und Bereich, Spalten je Gesellschaft."""
    companies = report["companies"]
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Bereich",
            "Konto",
            "Bezeichnung",
            "Kontoart",
            *[company["name"] for company in companies],
            "Summe",
            "Eliminierung",
            "Manuelle Buchung",
            "Konsolidiert",
        ]
    )

    def write_row(section: str, code: str, name: str, account_type: str, row: dict) -> None:
        writer.writerow(
            [
                section,
                code,
                name,
                account_type,
                *[f"{row['amounts'][company['company_id']]:.2f}" for company in companies],
                f"{row['total']:.2f}",
                f"{row['eliminations']:.2f}",
                f"{row['adjustments']:.2f}",
                f"{row['amount']:.2f}",
            ]
        )

    def write_section(section: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        for row in rows:
            write_row(section, row["code"], row["name"], row["account_type"] or "", row)
        write_row(section, "", "Summe", "", summary)

    trial_balance = report["trial_balance"]
    income_statement = report["income_statement"]
    balance_sheet = report["balance_sheet"]
    write_section("SuSa (Saldo Soll-Haben)", trial_balance["rows"], trial_balance["summary"])
    write_section(
        "GuV Erträge", income_statement["revenues"], income_statement["summary"]["revenues"]
    )
    write_section(
        "GuV Aufwendungen", income_statement["expenses"], income_statement["summary"]["expenses"]
    )
    write_row("GuV", "", "Ergebnis", "", income_statement["summary"]["net_income"])
    write_section("Bilanz Aktiva", balance_sheet["assets"], balance_sheet["summary"]["assets"])
    write_section(
        "Bilanz Passiva",
        balance_sheet["liabilities_and_equity"],
        balance_sheet["summary"]["liabilities_and_equity"],
    )
    return buffer.getvalue()
