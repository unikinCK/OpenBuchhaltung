from decimal import Decimal

import pytest

from domain.services.journal_entry_validation import (
    JournalEntryDraft,
    JournalEntryValidationError,
    JournalEntryValidator,
    ValidationLine,
)


def _line(debit: str, credit: str) -> ValidationLine:
    return ValidationLine(account_id=1, debit_amount=Decimal(debit), credit_amount=Decimal(credit))


def test_validator_accepts_balanced_posted_entry() -> None:
    draft = JournalEntryDraft(
        status="posted",
        lines=[_line("100.00", "0.00"), _line("0.00", "100.00")],
    )

    JournalEntryValidator.validate(draft)


def test_validator_rejects_unbalanced_entry() -> None:
    draft = JournalEntryDraft(
        status="posted",
        lines=[_line("100.00", "0.00"), _line("0.00", "99.00")],
    )

    with pytest.raises(JournalEntryValidationError, match="Soll und Haben"):
        JournalEntryValidator.validate(draft)


def test_validator_rejects_single_line_entry() -> None:
    draft = JournalEntryDraft(status="draft", lines=[_line("100.00", "0.00")])

    with pytest.raises(JournalEntryValidationError, match="mindestens zwei"):
        JournalEntryValidator.validate(draft)


def test_validator_rejects_unknown_status() -> None:
    draft = JournalEntryDraft(
        status="invalid",
        lines=[_line("100.00", "0.00"), _line("0.00", "100.00")],
    )

    with pytest.raises(JournalEntryValidationError, match="Ungültiger Status"):
        JournalEntryValidator.validate(draft)


def test_validator_rejects_zero_line_amount() -> None:
    draft = JournalEntryDraft(
        status="draft",
        lines=[_line("100.00", "0.00"), _line("0.00", "0.00")],
    )

    with pytest.raises(JournalEntryValidationError, match="Betrag muss größer 0"):
        JournalEntryValidator.validate(draft)
