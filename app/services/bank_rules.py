"""Auto-Kontierungsregeln für Bankumsätze.

Eine Regel ordnet Umsätzen anhand eines Teilstrings in Verwendungszweck
oder Gegenseite ein Gegenkonto (plus optional Steuercode und Controlling-
Dimensionen) zu. Regeln liefern Vorschläge auf der Bank-Seite und können
per Regel-Lauf alle offenen Treffer direkt verbuchen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.services.audit_log import log_audit_event
from app.services.bank_import import BankImportError, book_transaction
from app.services.journal_entries import JournalEntryCreationError
from domain.models import (
    Account,
    BankBookingRule,
    BankTransaction,
    Company,
    ControllingUnit,
    TaxCode,
)
from domain.services.journal_entry_validation import JournalEntryValidationError


class BankRuleError(ValueError):
    """Raised when a booking rule is invalid or cannot be applied."""


@dataclass
class RuleRunReport:
    """Ergebnis eines Regel-Laufs über die offenen Umsätze."""

    matched: int = 0
    booked: int = 0
    errors: list[str] = field(default_factory=list)


def serialize_rule(rule: BankBookingRule) -> dict[str, object]:
    return {
        "id": rule.id,
        "company_id": rule.company_id,
        "pattern": rule.pattern,
        "contra_account_id": rule.contra_account_id,
        "tax_code_id": rule.tax_code_id,
        "cost_center_id": rule.cost_center_id,
        "profit_center_id": rule.profit_center_id,
        "is_active": rule.is_active,
        "created_at": rule.created_at.isoformat(),
    }


def create_rule(
    *,
    session: Session,
    company_id: int,
    pattern: str,
    contra_account_id: int,
    tax_code_id: int | None = None,
    cost_center_id: int | None = None,
    profit_center_id: int | None = None,
    changed_by: str,
) -> BankBookingRule:
    company = session.get(Company, company_id)
    if company is None:
        raise BankRuleError("Gesellschaft nicht gefunden.")

    pattern = " ".join(pattern.split())
    if len(pattern) < 3:
        raise BankRuleError("Das Muster muss mindestens 3 Zeichen lang sein.")

    contra_account = session.get(Account, contra_account_id)
    if contra_account is None or contra_account.company_id != company.id:
        raise BankRuleError("Gegenkonto nicht gefunden.")
    if tax_code_id is not None:
        tax_code = session.get(TaxCode, tax_code_id)
        if tax_code is None or tax_code.company_id != company.id:
            raise BankRuleError("Steuercode nicht gefunden.")
    for unit_id, expected in ((cost_center_id, "cost_center"), (profit_center_id, "profit_center")):
        if unit_id is None:
            continue
        unit = session.get(ControllingUnit, unit_id)
        if unit is None or unit.company_id != company.id or unit.unit_type != expected:
            raise BankRuleError("Controlling-Einheit nicht gefunden.")

    rule = BankBookingRule(
        tenant_id=company.tenant_id,
        company_id=company.id,
        pattern=pattern,
        contra_account_id=contra_account_id,
        tax_code_id=tax_code_id,
        cost_center_id=cost_center_id,
        profit_center_id=profit_center_id,
    )
    session.add(rule)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise BankRuleError("Für dieses Muster existiert bereits eine Regel.") from exc

    log_audit_event(
        session=session,
        tenant_id=company.tenant_id,
        company_id=company.id,
        entity_type="bank_booking_rule",
        entity_id=str(rule.id),
        action="created",
        changed_by=changed_by,
        payload={"pattern": pattern, "contra_account_id": contra_account_id},
    )
    session.commit()
    session.refresh(rule)
    return rule


def list_rules(
    *, session: Session, company_id: int, include_inactive: bool = False
) -> list[BankBookingRule]:
    stmt = (
        select(BankBookingRule)
        .where(BankBookingRule.company_id == company_id)
        .options(
            selectinload(BankBookingRule.contra_account),
            selectinload(BankBookingRule.tax_code),
        )
        .order_by(BankBookingRule.pattern)
    )
    if not include_inactive:
        stmt = stmt.where(BankBookingRule.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def set_rule_active(
    *, session: Session, rule_id: int, is_active: bool, changed_by: str
) -> BankBookingRule:
    rule = session.get(BankBookingRule, rule_id)
    if rule is None:
        raise BankRuleError("Regel nicht gefunden.")
    rule.is_active = is_active
    log_audit_event(
        session=session,
        tenant_id=rule.tenant_id,
        company_id=rule.company_id,
        entity_type="bank_booking_rule",
        entity_id=str(rule.id),
        action="activated" if is_active else "deactivated",
        changed_by=changed_by,
        payload={"pattern": rule.pattern},
    )
    session.commit()
    session.refresh(rule)
    return rule


def match_rules_for(
    *, session: Session, transactions: Sequence[BankTransaction]
) -> dict[int, BankBookingRule]:
    """Passende Regel je offenem Umsatz; das längste Muster gewinnt."""
    open_transactions = [t for t in transactions if t.status == "open"]
    if not open_transactions:
        return {}

    rules_by_company: dict[int, list[BankBookingRule]] = {}
    for company_id in {t.company_id for t in open_transactions}:
        rules = list_rules(session=session, company_id=company_id)
        rules_by_company[company_id] = sorted(
            rules, key=lambda rule: len(rule.pattern), reverse=True
        )

    matches: dict[int, BankBookingRule] = {}
    for transaction in open_transactions:
        haystack = f"{transaction.purpose} {transaction.counterparty or ''}".lower()
        for rule in rules_by_company.get(transaction.company_id, []):
            if rule.pattern.lower() in haystack:
                matches[transaction.id] = rule
                break
    return matches


def apply_rules(
    *,
    session: Session,
    company_id: int,
    changed_by: str,
    transaction_ids: Sequence[int] | None = None,
) -> RuleRunReport:
    """Verbucht alle offenen Umsätze mit Regel-Treffer (optional eingeschränkt
    auf einzelne Umsätze). Fehler einzelner Buchungen (z. B. gesperrte
    Periode) brechen den Lauf nicht ab, sondern landen im Report.
    """
    stmt = select(BankTransaction).where(
        BankTransaction.company_id == company_id,
        BankTransaction.status == "open",
    )
    if transaction_ids:
        stmt = stmt.where(BankTransaction.id.in_(list(transaction_ids)))
    transactions = (
        session.execute(stmt.order_by(BankTransaction.booking_date, BankTransaction.id))
        .scalars()
        .all()
    )

    matches = match_rules_for(session=session, transactions=transactions)
    report = RuleRunReport(matched=len(matches))
    for transaction in transactions:
        rule = matches.get(transaction.id)
        if rule is None:
            continue
        try:
            book_transaction(
                session=session,
                transaction_id=transaction.id,
                contra_account_id=rule.contra_account_id,
                tax_code_id=rule.tax_code_id,
                cost_center_id=rule.cost_center_id,
                profit_center_id=rule.profit_center_id,
                description=f"Bank: {transaction.purpose} (Regel: {rule.pattern})",
                changed_by=changed_by,
            )
            report.booked += 1
        except (BankImportError, JournalEntryCreationError, JournalEntryValidationError) as exc:
            session.rollback()
            report.errors.append(
                f"Umsatz {transaction.id} ({transaction.booking_date.isoformat()}, "
                f"{transaction.amount}): {exc}"
            )
    return report
