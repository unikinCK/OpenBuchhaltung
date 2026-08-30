"""Buchungsvorlagen: wiederkehrende Buchungen anlegen und ausführen.

Eine Vorlage speichert die Buchungszeilen einer typischen Buchung (Miete,
Versicherung, Abo). ``interval`` steuert die Wiedervorlage: fällige Vorlagen
werden auf der Buchungsseite angeboten; das Buchen erzeugt eine normale
Journalbuchung und schiebt ``next_run`` um das Intervall weiter.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services.audit_log import log_audit_event
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from domain.models import Account, Company, JournalEntry, JournalTemplate

INTERVALS = ("on_demand", "monthly", "quarterly", "yearly")
_INTERVAL_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}


class JournalTemplateError(ValueError):
    """Raised when a journal template is invalid or cannot be booked."""


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    # Monatsende sauber behandeln (31. Jan + 1 Monat -> 28./29. Feb).
    for dom in (day.day, 30, 29, 28):
        try:
            return date(year, month, dom)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _normalized_lines(session: Session, company: Company, raw_lines: list) -> list[dict]:
    if not isinstance(raw_lines, list) or len(raw_lines) < 2:
        raise JournalTemplateError("Eine Vorlage braucht mindestens zwei Buchungszeilen.")

    lines: list[dict] = []
    for index, raw in enumerate(raw_lines, start=1):
        if not isinstance(raw, dict):
            raise JournalTemplateError(f"Zeile {index}: ungültiges Format.")
        try:
            account_id = int(raw.get("account_id") or 0)
            debit = Decimal(str(raw.get("debit") or "0")).quantize(Decimal("0.01"))
            credit = Decimal(str(raw.get("credit") or "0")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise JournalTemplateError(f"Zeile {index}: ungültige Beträge.") from exc

        account = session.get(Account, account_id)
        if account is None or account.company_id != company.id:
            raise JournalTemplateError(f"Zeile {index}: Konto nicht gefunden.")
        if debit < 0 or credit < 0 or (debit > 0) == (credit > 0):
            raise JournalTemplateError(
                f"Zeile {index}: genau eine Seite (Soll oder Haben) muss größer 0 sein."
            )

        line = {
            "account_id": account_id,
            "debit": str(debit),
            "credit": str(credit),
        }
        for optional in ("tax_code_id", "cost_center_id", "profit_center_id"):
            value = raw.get(optional)
            if value not in (None, "", 0):
                line[optional] = int(value)
        if raw.get("description"):
            line["description"] = str(raw["description"])[:255]
        lines.append(line)
    return lines


def create_template(
    *,
    session: Session,
    company_id: int,
    name: str,
    description: str,
    lines: list,
    interval: str = "on_demand",
    next_run: date | None = None,
    changed_by: str,
) -> JournalTemplate:
    company = session.get(Company, company_id)
    if company is None:
        raise JournalTemplateError("Gesellschaft nicht gefunden.")

    name = " ".join(name.split())
    if not name:
        raise JournalTemplateError("Name der Vorlage fehlt.")
    description = description.strip() or name
    if interval not in INTERVALS:
        raise JournalTemplateError(
            "Intervall muss on_demand, monthly, quarterly oder yearly sein."
        )
    if interval != "on_demand" and next_run is None:
        next_run = date.today()
    if interval == "on_demand":
        next_run = None

    template = JournalTemplate(
        tenant_id=company.tenant_id,
        company_id=company.id,
        name=name,
        description=description,
        interval=interval,
        next_run=next_run,
        lines=_normalized_lines(session, company, lines),
    )
    session.add(template)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise JournalTemplateError("Eine Vorlage mit diesem Namen existiert bereits.") from exc

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="journal_template",
        entity_id=str(template.id),
        action="created",
        changed_by=changed_by,
        payload={"name": name, "interval": interval, "line_count": len(template.lines)},
    )
    session.commit()
    session.refresh(template)
    return template


def list_templates(
    *, session: Session, company_id: int, include_inactive: bool = False
) -> list[JournalTemplate]:
    stmt = (
        select(JournalTemplate)
        .where(JournalTemplate.company_id == company_id)
        .order_by(JournalTemplate.name)
    )
    if not include_inactive:
        stmt = stmt.where(JournalTemplate.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def due_templates(
    *, session: Session, company_id: int, as_of: date | None = None
) -> list[JournalTemplate]:
    as_of = as_of or date.today()
    return [
        template
        for template in list_templates(session=session, company_id=company_id)
        if template.next_run is not None and template.next_run <= as_of
    ]


def set_template_active(
    *, session: Session, template_id: int, is_active: bool, changed_by: str
) -> JournalTemplate:
    template = session.get(JournalTemplate, template_id)
    if template is None:
        raise JournalTemplateError("Vorlage nicht gefunden.")
    template.is_active = is_active
    log_audit_event(
        session=session,
        tenant_id=template.tenant_id,
        company_id=template.company_id,
        entity_type="journal_template",
        entity_id=str(template.id),
        action="activated" if is_active else "deactivated",
        changed_by=changed_by,
        payload={"name": template.name},
    )
    session.commit()
    session.refresh(template)
    return template


def book_template(
    *,
    session: Session,
    template_id: int,
    entry_date: date | None = None,
    changed_by: str,
) -> tuple[JournalEntry, JournalTemplate]:
    """Bucht eine Vorlage als normale Journalbuchung und schiebt next_run weiter."""
    template = session.get(JournalTemplate, template_id)
    if template is None:
        raise JournalTemplateError("Vorlage nicht gefunden.")
    if not template.is_active:
        raise JournalTemplateError("Die Vorlage ist deaktiviert.")

    entry_date = entry_date or template.next_run or date.today()
    lines = [
        JournalLineInput(
            account_id=line["account_id"],
            debit_amount=Decimal(line["debit"]),
            credit_amount=Decimal(line["credit"]),
            tax_code_id=line.get("tax_code_id"),
            cost_center_id=line.get("cost_center_id"),
            profit_center_id=line.get("profit_center_id"),
            description=line.get("description"),
        )
        for line in template.lines
    ]
    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=template.company_id,
            entry_date=entry_date,
            description=template.description,
            status="posted",
            changed_by=changed_by,
            lines=lines,
        ),
        commit=False,
    )

    if template.interval in _INTERVAL_MONTHS:
        base = template.next_run or entry_date
        template.next_run = _add_months(base, _INTERVAL_MONTHS[template.interval])

    log_audit_event(
        session=session,
        tenant_id=template.tenant_id,
        company_id=template.company_id,
        entity_type="journal_template",
        entity_id=str(template.id),
        action="booked",
        changed_by=changed_by,
        payload={
            "journal_entry_id": entry.id,
            "posting_number": entry.posting_number,
            "entry_date": entry_date.isoformat(),
            "next_run": template.next_run.isoformat() if template.next_run else None,
        },
    )
    session.commit()
    session.refresh(entry)
    session.refresh(template)
    return entry, template


def serialize_template(template: JournalTemplate) -> dict[str, object]:
    return {
        "id": template.id,
        "company_id": template.company_id,
        "name": template.name,
        "description": template.description,
        "interval": template.interval,
        "next_run": template.next_run.isoformat() if template.next_run else None,
        "lines": template.lines,
        "is_active": template.is_active,
        "created_at": template.created_at.isoformat(),
    }
