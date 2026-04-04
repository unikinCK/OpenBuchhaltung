from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class JournalEntryValidationError(ValueError):
    """Raised when a JournalEntry violates business validation rules."""


@dataclass(slots=True)
class ValidationLine:
    debit_amount: Decimal
    credit_amount: Decimal


@dataclass(slots=True)
class JournalEntryDraft:
    status: str
    lines: list[ValidationLine]


class JournalEntryValidator:
    ALLOWED_STATUSES = {"draft", "posted"}

    @classmethod
    def validate(cls, entry: JournalEntryDraft) -> None:
        if entry.status not in cls.ALLOWED_STATUSES:
            raise JournalEntryValidationError(
                "Ungültiger Status. Erlaubt sind nur 'draft' oder 'posted'."
            )

        if len(entry.lines) < 2:
            raise JournalEntryValidationError(
                "Eine Buchung benötigt mindestens zwei Buchungszeilen."
            )

        debit_total = sum((line.debit_amount for line in entry.lines), Decimal("0.00"))
        credit_total = sum((line.credit_amount for line in entry.lines), Decimal("0.00"))

        if debit_total != credit_total:
            raise JournalEntryValidationError(
                "Soll und Haben müssen identisch sein "
                f"(Soll: {debit_total:.2f}, Haben: {credit_total:.2f})."
            )

        if entry.status == "posted" and debit_total == Decimal("0.00"):
            raise JournalEntryValidationError(
                "Eine gebuchte Buchung benötigt einen Betrag größer 0."
            )
