from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models import Account


def parent_code_candidates(code: str) -> list[str]:
    digits = "".join(char for char in code if char.isdigit())
    padded = (digits[:4]).ljust(4, "0")
    candidates: list[str] = []
    variants = [
        f"{padded[0]}{padded[1]}{padded[2]}0",
        f"{padded[0]}{padded[1]}00",
        f"{padded[0]}000",
    ]
    for candidate in variants:
        if candidate != padded and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def resolve_parent_account_id(*, session: Session, company_id: int, code: str) -> int | None:
    candidates = parent_code_candidates(code)
    if not candidates:
        return None

    accounts = {
        account.code: account.id
        for account in session.execute(
            select(Account).where(Account.company_id == company_id, Account.code.in_(candidates))
        ).scalars()
    }
    for code_candidate in candidates:
        if code_candidate in accounts:
            return accounts[code_candidate]
    return None
