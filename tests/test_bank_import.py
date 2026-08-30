from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import create_app
from app.auth import hash_password
from app.services.bank_import import (
    BankImportError,
    book_transaction,
    import_bank_csv,
    match_transaction,
    move_bank_transactions,
    net_from_gross,
    reassign_bank_transactions,
    suggest_matches,
)
from app.services.journal_entries import (
    JournalEntryInput,
    JournalLineInput,
    create_journal_entry,
)
from app.services.tax_codes import ensure_default_tax_codes
from domain.models import (
    Account,
    AuditLog,
    BankTransaction,
    Base,
    Company,
    ControllingUnit,
    JournalEntryLine,
    TaxCode,
    Tenant,
    User,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session


def _seed_company(session: Session) -> tuple[Company, Account, Account]:
    tenant = Tenant(name="Bank Tenant")
    company = Company(tenant=tenant, name="Bank GmbH", currency_code="EUR")
    session.add_all([tenant, company])
    session.flush()

    bank = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1200",
        name="Bank",
        account_type="asset",
    )
    rent = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="4200",
        name="Miete",
        account_type="expense",
    )
    vat_in = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="1576",
        name="Vorsteuer 19 %",
        account_type="asset",
    )
    revenue = Account(
        tenant_id=tenant.id,
        company_id=company.id,
        code="8400",
        name="Erlöse",
        account_type="income",
    )
    session.add_all([bank, rent, vat_in, revenue])
    session.commit()
    return company, bank, rent


GERMAN_CSV = """Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag
05.07.2026;Zahlungseingang RE-1001;Kunde AG;1.190,00
06.07.2026;Miete Juli;Vermieter GmbH;-595,00
07.07.2026;Gebühren;Hausbank;-9,90
"""


def test_import_bank_csv_german_format_and_dedup(session: Session) -> None:
    company, bank, _ = _seed_company(session)

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert report.imported_rows == 3
    assert report.error_rows == 0

    transactions = session.execute(
        select(BankTransaction).order_by(BankTransaction.booking_date)
    ).scalars().all()
    assert transactions[0].amount == Decimal("1190.00")
    assert transactions[0].booking_date == date(2026, 7, 5)
    assert transactions[1].amount == Decimal("-595.00")
    assert transactions[0].status == "open"

    # Re-Import ist idempotent
    second = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert second.imported_rows == 0
    assert second.duplicate_rows == 3

    audit = session.execute(
        select(AuditLog).where(AuditLog.entity_type == "bank_import")
    ).scalars().all()
    assert len(audit) == 2


def test_parse_amount_handles_german_and_english_formats() -> None:
    from app.services.bank_import import _parse_amount

    assert _parse_amount("1.234,56") == Decimal("1234.56")
    assert _parse_amount("1,234.56") == Decimal("1234.56")
    assert _parse_amount("-1,000.00") == Decimal("-1000.00")
    assert _parse_amount("1.234.567,89") == Decimal("1234567.89")
    assert _parse_amount("1.234.567") == Decimal("1234567.00")
    assert _parse_amount("-9,90") == Decimal("-9.90")
    assert _parse_amount("1234.56") == Decimal("1234.56")

    for ambiguous in ("1,234", "1.234"):
        with pytest.raises(BankImportError):
            _parse_amount(ambiguous)
    with pytest.raises(BankImportError):
        _parse_amount("abc")


def test_import_keeps_identical_rows_within_one_file(session: Session) -> None:
    """Zwei echte, identisch aussehende Zahlungen ohne Referenz bleiben erhalten."""
    company, bank, _ = _seed_company(session)
    twin_csv = (
        "Buchungstag;Verwendungszweck;Auftraggeber/Empfänger;Betrag\n"
        "05.07.2026;Kartenzahlung;Baecker;-3,50\n"
        "05.07.2026;Kartenzahlung;Baecker;-3,50\n"
    )

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(twin_csv),
        changed_by="tester",
    )
    assert report.imported_rows == 2
    assert report.duplicate_rows == 0

    second = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(twin_csv),
        changed_by="tester",
    )
    assert second.imported_rows == 0
    assert second.duplicate_rows == 2


def test_parallel_import_conflict_is_reported_as_duplicates(
    session: Session, monkeypatch
) -> None:
    """Verliert der Import das Rennen um den Unique-Constraint, zählt die Zeile als Duplikat."""
    from app.services import bank_import as bank_import_module

    company, bank, _ = _seed_company(session)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )

    real_prefetch = bank_import_module._existing_hashes_for
    calls = {"count": 0}

    def stale_then_real(**kwargs):
        calls["count"] += 1
        # Erster Aufruf simuliert einen veralteten Lesestand (paralleler Import).
        return set() if calls["count"] == 1 else real_prefetch(**kwargs)

    monkeypatch.setattr(bank_import_module, "_existing_hashes_for", stale_then_real)

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.duplicate_rows == 3
    assert calls["count"] == 2
    assert len(session.execute(select(BankTransaction)).scalars().all()) == 3


def test_import_reports_row_errors(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    broken_csv = "Buchungstag;Verwendungszweck;Betrag\nkein-datum;Test;10,00\n05.07.2026;;5,00\n"

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(broken_csv),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.error_rows == 2


def test_suggest_and_match_transaction(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    revenue_id = session.execute(
        select(Account.id).where(Account.company_id == company.id, Account.code == "8400")
    ).scalar_one()

    entry = create_journal_entry(
        session=session,
        payload=JournalEntryInput(
            company_id=company.id,
            entry_date=date(2026, 7, 4),
            description="Ausgangsrechnung RE-1001",
            status="posted",
            lines=[
                JournalLineInput(bank.id, Decimal("1190.00"), Decimal("0.00")),
                JournalLineInput(revenue_id, Decimal("0.00"), Decimal("1190.00")),
            ],
        ),
    )

    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    incoming = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("1190.00"))
    ).scalar_one()

    suggestions = suggest_matches(session=session, transaction=incoming)
    assert [suggestion.id for suggestion in suggestions] == [entry.id]

    matched = match_transaction(
        session=session,
        transaction_id=incoming.id,
        journal_entry_id=entry.id,
        changed_by="tester",
    )
    assert matched.status == "matched"
    assert matched.journal_entry_id == entry.id

    # Bereits verknüpfte Buchungen werden nicht mehr vorgeschlagen
    assert suggest_matches(session=session, transaction=incoming) == []

    with pytest.raises(BankImportError, match="bereits zugeordnet"):
        match_transaction(
            session=session,
            transaction_id=incoming.id,
            journal_entry_id=entry.id,
            changed_by="tester",
        )

    # Dieselbe Buchung darf keinem zweiten Bankumsatz zugeordnet werden.
    other = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()
    with pytest.raises(BankImportError, match="anderen Bankumsatz"):
        match_transaction(
            session=session,
            transaction_id=other.id,
            journal_entry_id=entry.id,
            changed_by="tester",
        )


def _second_bank_account(session: Session, company: Company) -> Account:
    account = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1230",
        name="Zweitbank",
        account_type="asset",
    )
    session.add(account)
    session.commit()
    return account


def test_move_bank_transactions_updates_account_and_dedup_hash(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    target = _second_bank_account(session, company)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    hashes_before = set(session.execute(select(BankTransaction.dedup_hash)).scalars())

    result = move_bank_transactions(
        session=session,
        company_id=company.id,
        source_bank_account_id=bank.id,
        target_bank_account_id=target.id,
        changed_by="tester",
    )
    moved = result.transactions
    assert len(moved) == 3
    assert {transaction.bank_account_id for transaction in moved} == {target.id}
    # Keine verbuchten Umsätze bewegt -> keine Umgliederungsbuchung nötig.
    assert result.reclassification_entry is None

    # Der Dedup-Hash enthält das Bankkonto und wird mitgeführt, damit ein
    # Re-Import auf dem Zielkonto weiterhin als Duplikat erkannt wird.
    hashes_after = set(session.execute(select(BankTransaction.dedup_hash)).scalars())
    assert hashes_after.isdisjoint(hashes_before)

    report = import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=target.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    assert report.imported_rows == 0
    assert report.duplicate_rows == 3

    audit = (
        session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "bank_transaction", AuditLog.action == "reassigned"
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 3
    assert audit[0].payload["from_bank_account_id"] == bank.id
    assert audit[0].payload["to_bank_account_id"] == target.id

    with pytest.raises(BankImportError, match="identisch"):
        move_bank_transactions(
            session=session,
            company_id=company.id,
            source_bank_account_id=target.id,
            target_bank_account_id=target.id,
            changed_by="tester",
        )

    # Ein bereits auf dem Zielkonto liegender Umsatz ist ein No-op.
    assert not reassign_bank_transactions(
        session=session,
        transaction_ids=[moved[0].id],
        bank_account_id=target.id,
        changed_by="tester",
    )


def test_book_transaction_rejects_foreign_currency(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()
    outgoing.currency_code = "USD"
    session.commit()

    with pytest.raises(BankImportError, match="USD"):
        book_transaction(
            session=session,
            transaction_id=outgoing.id,
            contra_account_id=rent.id,
            changed_by="tester",
        )
    session.refresh(outgoing)
    assert outgoing.status == "open"


def test_move_bank_transactions_keeps_existing_posting(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    target = _second_bank_account(session, company)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()
    booked = book_transaction(
        session=session,
        transaction_id=outgoing.id,
        contra_account_id=rent.id,
        changed_by="tester",
    )
    entry_id = booked.journal_entry_id

    result = reassign_bank_transactions(
        session=session,
        transaction_ids=[outgoing.id],
        bank_account_id=target.id,
        changed_by="tester",
    )
    session.refresh(outgoing)
    assert outgoing.bank_account_id == target.id
    assert outgoing.journal_entry_id == entry_id

    # Die Buchung selbst bleibt nach GoBD unverändert auf dem alten Bankkonto.
    bank_line_accounts = set(
        session.execute(
            select(JournalEntryLine.account_id).where(
                JournalEntryLine.journal_entry_id == entry_id
            )
        ).scalars()
    )
    assert bank.id in bank_line_accounts
    assert target.id not in bank_line_accounts

    # Der Saldo wird automatisch per Umgliederungsbuchung mitgezogen:
    # -595 auf dem Quellkonto -> Quellkonto Soll 595, Zielkonto Haben 595.
    reclass = result.reclassification_entry
    assert reclass is not None
    assert "Umgliederung" in reclass.description
    reclass_lines = {
        row.account_id: (row.debit_amount, row.credit_amount)
        for row in session.execute(
            select(
                JournalEntryLine.account_id,
                JournalEntryLine.debit_amount,
                JournalEntryLine.credit_amount,
            ).where(JournalEntryLine.journal_entry_id == reclass.id)
        )
    }
    assert reclass_lines[bank.id] == (Decimal("595.00"), Decimal("0.00"))
    assert reclass_lines[target.id] == (Decimal("0.00"), Decimal("595.00"))

    # Ohne reclassify entsteht keine Buchung (Rückweg zum Ursprungskonto).
    back = reassign_bank_transactions(
        session=session,
        transaction_ids=[outgoing.id],
        bank_account_id=bank.id,
        changed_by="tester",
        reclassify=False,
    )
    assert back.reclassification_entry is None


def test_reassign_rejects_unknown_and_non_asset_accounts(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    transaction_id = session.execute(select(BankTransaction.id)).scalars().first()

    with pytest.raises(BankImportError, match="Kontoart asset"):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[transaction_id],
            bank_account_id=rent.id,
            changed_by="tester",
        )
    with pytest.raises(BankImportError, match="Bankkonto nicht gefunden"):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[transaction_id],
            bank_account_id=9999,
            changed_by="tester",
        )
    with pytest.raises(BankImportError, match="Bankumsatz nicht gefunden"):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[9999],
            bank_account_id=bank.id,
            changed_by="tester",
        )
    with pytest.raises(BankImportError, match="Kein Bankumsatz"):
        reassign_bank_transactions(
            session=session,
            transaction_ids=[],
            bank_account_id=bank.id,
            changed_by="tester",
        )


def test_move_bank_transactions_detects_duplicate_on_target(session: Session) -> None:
    company, bank, _ = _seed_company(session)
    target = _second_bank_account(session, company)
    for account_id in (bank.id, target.id):
        import_bank_csv(
            session=session,
            company_id=company.id,
            bank_account_id=account_id,
            csv_stream=StringIO(GERMAN_CSV),
            changed_by="tester",
        )

    # Derselbe Umsatz liegt bereits auf dem Zielkonto — sonst würde der
    # Umzug die Dedup-Eindeutigkeit verletzen.
    with pytest.raises(BankImportError, match="bereits vorhanden"):
        move_bank_transactions(
            session=session,
            company_id=company.id,
            source_bank_account_id=bank.id,
            target_bank_account_id=target.id,
            changed_by="tester",
        )
    session.rollback()
    assert (
        session.execute(
            select(BankTransaction).where(BankTransaction.bank_account_id == bank.id)
        )
        .scalars()
        .all()
        != []
    )


def test_move_creates_aggregated_reclassification_entry(session: Session) -> None:
    """Mehrere verbuchte Umsätze ergeben eine Umgliederungsbuchung über den Netto-Saldo."""
    company, bank, rent = _seed_company(session)
    target = _second_bank_account(session, company)
    revenue_id = session.execute(
        select(Account.id).where(Account.company_id == company.id, Account.code == "8400")
    ).scalar_one()
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    incoming = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("1190.00"))
    ).scalar_one()
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()
    book_transaction(
        session=session,
        transaction_id=incoming.id,
        contra_account_id=revenue_id,
        changed_by="tester",
    )
    book_transaction(
        session=session,
        transaction_id=outgoing.id,
        contra_account_id=rent.id,
        changed_by="tester",
    )

    result = move_bank_transactions(
        session=session,
        company_id=company.id,
        source_bank_account_id=bank.id,
        target_bank_account_id=target.id,
        changed_by="tester",
    )
    assert len(result.transactions) == 3
    reclass = result.reclassification_entry
    assert reclass is not None

    # Netto verbucht: +1190 - 595 = +595 -> Quellkonto Haben, Zielkonto Soll.
    lines = {
        row.account_id: (row.debit_amount, row.credit_amount)
        for row in session.execute(
            select(
                JournalEntryLine.account_id,
                JournalEntryLine.debit_amount,
                JournalEntryLine.credit_amount,
            ).where(JournalEntryLine.journal_entry_id == reclass.id)
        )
    }
    assert lines[bank.id] == (Decimal("0.00"), Decimal("595.00"))
    assert lines[target.id] == (Decimal("595.00"), Decimal("0.00"))


def test_find_geldtransit_account_and_transfer_detection(session: Session) -> None:
    from app.services.bank_import import (
        detect_transfer_counterparts,
        find_geldtransit_account,
    )

    company, bank, _ = _seed_company(session)
    target = _second_bank_account(session, company)

    assert find_geldtransit_account(session=session, company_id=company.id) is None
    transit = Account(
        tenant_id=company.tenant_id,
        company_id=company.id,
        code="1360",
        name="Geldtransit",
        account_type="asset",
    )
    session.add(transit)
    session.commit()
    assert find_geldtransit_account(session=session, company_id=company.id).id == transit.id

    def _tx(account_id: int, amount: str, day: int, purpose: str, suffix: str):
        return BankTransaction(
            tenant_id=company.tenant_id,
            company_id=company.id,
            bank_account_id=account_id,
            booking_date=date(2026, 7, day),
            amount=Decimal(amount),
            currency_code="EUR",
            purpose=purpose,
            dedup_hash=f"transfer-{suffix}",
        )

    out_a = _tx(bank.id, "-1000.00", 10, "Übertrag Sparkonto", "a")
    in_b = _tx(target.id, "1000.00", 11, "Übertrag von Girokonto", "b")
    far_away = _tx(target.id, "1000.00", 20, "Anderer Eingang", "c")
    same_account = _tx(bank.id, "1000.00", 10, "Erstattung", "d")
    session.add_all([out_a, in_b, far_away, same_account])
    session.commit()

    matches = detect_transfer_counterparts(
        session=session, transactions=[out_a, in_b, far_away]
    )
    # out_a und in_b sind gegenläufig innerhalb der Toleranz und paaren sich;
    # far_away (9 Tage Abstand) bekommt kein Gegenstück auf dem anderen Konto.
    assert matches[out_a.id].id == in_b.id
    assert matches[in_b.id].id == out_a.id
    assert far_away.id not in matches
    # same_account liegt auf demselben Konto wie out_a und ist kein Gegenstück.
    assert matches[out_a.id].bank_account_id != out_a.bank_account_id


def test_move_bank_transactions_filters_by_status(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    target = _second_bank_account(session, company)
    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()
    book_transaction(
        session=session,
        transaction_id=outgoing.id,
        contra_account_id=rent.id,
        changed_by="tester",
    )

    moved = move_bank_transactions(
        session=session,
        company_id=company.id,
        source_bank_account_id=bank.id,
        target_bank_account_id=target.id,
        statuses=["open"],
        changed_by="tester",
    ).transactions
    assert len(moved) == 2
    session.refresh(outgoing)
    assert outgoing.bank_account_id == bank.id


def test_book_transaction_with_tax_code_splits_gross(session: Session) -> None:
    company, bank, rent = _seed_company(session)
    cost_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="cost_center",
        code="K100",
        name="Verwaltung",
    )
    profit_center = ControllingUnit(
        tenant_id=company.tenant_id,
        company_id=company.id,
        unit_type="profit_center",
        code="P100",
        name="Zentrale",
    )
    session.add_all([cost_center, profit_center])
    session.commit()
    ensure_default_tax_codes(session=session, company=company)
    vst19 = session.execute(
        select(TaxCode).where(TaxCode.company_id == company.id, TaxCode.code == "VSt19")
    ).scalar_one()

    import_bank_csv(
        session=session,
        company_id=company.id,
        bank_account_id=bank.id,
        csv_stream=StringIO(GERMAN_CSV),
        changed_by="tester",
    )
    outgoing = session.execute(
        select(BankTransaction).where(BankTransaction.amount == Decimal("-595.00"))
    ).scalar_one()

    booked = book_transaction(
        session=session,
        transaction_id=outgoing.id,
        contra_account_id=rent.id,
        tax_code_id=vst19.id,
        cost_center_id=cost_center.id,
        profit_center_id=profit_center.id,
        changed_by="tester",
    )
    assert booked.status == "booked"

    lines = session.execute(
        select(JournalEntryLine)
        .where(JournalEntryLine.journal_entry_id == booked.journal_entry_id)
        .order_by(JournalEntryLine.line_number)
    ).scalars().all()
    # Bank 595 Haben, Miete 500 Soll, Vorsteuer 95 Soll
    assert len(lines) == 3
    assert lines[0].credit_amount == Decimal("595.00")
    assert lines[1].debit_amount == Decimal("500.00")
    assert lines[2].debit_amount == Decimal("95.00")
    assert lines[0].cost_center_id is None
    assert lines[0].profit_center_id is None
    assert all(line.cost_center_id == cost_center.id for line in lines[1:])
    assert all(line.profit_center_id == profit_center.id for line in lines[1:])


def test_net_from_gross_edge_cases() -> None:
    assert net_from_gross(Decimal("119.00"), Decimal("19.00")) == (
        Decimal("100.00"),
        Decimal("19.00"),
    )
    assert net_from_gross(Decimal("595.00"), Decimal("19.00")) == (
        Decimal("500.00"),
        Decimal("95.00"),
    )
    net, tax = net_from_gross(Decimal("0.02"), Decimal("19.00"))
    assert net + tax == Decimal("0.02")
    assert net_from_gross(Decimal("50.00"), Decimal("0.00")) == (
        Decimal("50.00"),
        Decimal("0.00"),
    )
    # 0,03 € hat keine exakte Netto+19%-Zerlegung — muss sauber fehlschlagen
    with pytest.raises(BankImportError, match="zerlegen"):
        net_from_gross(Decimal("0.03"), Decimal("19.00"))


def _create_ui_app(tmp_path: Path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'test_bank.db'}",
        }
    )
    with app.extensions["db_session_factory"]() as db_session:
        db_session.add(
            User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="Admin",
                tenant_id=None,
            )
        )
        db_session.commit()
    return app


def test_bank_page_upload_and_book_flow(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )

    upload_response = client.post(
        "/bank/import",
        data={
            "company_id": "1",
            "bank_account_id": "1",
            "bank_csv": (BytesIO(GERMAN_CSV.encode("utf-8")), "umsaetze.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert upload_response.status_code == 200
    assert b"3 neu" in upload_response.data
    assert b"Zahlungseingang RE-1001" in upload_response.data

    book_response = client.post(
        "/bank/2/buchen",
        data={"company_id": "1", "contra_account_id": "2"},
        follow_redirects=True,
    )
    assert book_response.status_code == 200
    assert b"wurde verbucht" in book_response.data
    assert b"verbucht</span>" in book_response.data


def test_bank_account_api_create_list_and_import(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )

    missing_fields = client.post("/api/v1/bank-accounts", json={"company_id": 1, "code": "1210"})
    assert missing_fields.status_code == 400

    created = client.post(
        "/api/v1/bank-accounts",
        json={"company_id": 1, "code": "1210", "name": "ING Girokonto"},
    )
    assert created.status_code == 201
    bank_account = created.get_json()
    assert bank_account["account_type"] == "asset"
    assert bank_account["code"] == "1210"

    duplicate = client.post(
        "/api/v1/bank-accounts",
        json={"company_id": 1, "code": "1210", "name": "Doppelt"},
    )
    assert duplicate.status_code == 409

    # Nur Sachkonten der Kontoart asset erscheinen als Bankkonten.
    list_response = client.get("/api/v1/bank-accounts", query_string={"company_id": 1})
    assert list_response.status_code == 200
    assert [account["code"] for account in list_response.get_json()["bank_accounts"]] == ["1210"]

    import_response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": 1,
            "bank_account_id": bank_account["id"],
            "file_name": "umsaetze.csv",
            "mime_type": "text/csv",
            "content_base64": base64.b64encode(GERMAN_CSV.encode("utf-8")).decode("ascii"),
        },
    )
    assert import_response.status_code == 201
    assert import_response.get_json()["report"]["imported_rows"] == 3


def test_bank_api_import_list_match_and_book_flow(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "8400", "name": "Erlöse", "account_type": "income"},
    )

    with app.extensions["db_session_factory"]() as db_session:
        bank_id = db_session.execute(select(Account.id).where(Account.code == "1200")).scalar_one()
        rent_id = db_session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()
        revenue_id = db_session.execute(
            select(Account.id).where(Account.code == "8400")
        ).scalar_one()
        matching_entry = create_journal_entry(
            session=db_session,
            payload=JournalEntryInput(
                company_id=1,
                entry_date=date(2026, 7, 4),
                description="Ausgangsrechnung RE-1001",
                status="posted",
                lines=[
                    JournalLineInput(bank_id, Decimal("1190.00"), Decimal("0.00")),
                    JournalLineInput(revenue_id, Decimal("0.00"), Decimal("1190.00")),
                ],
            ),
        )
        matching_entry_id = matching_entry.id

    import_response = client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": 1,
            "bank_account_id": bank_id,
            "file_name": "umsaetze.csv",
            "mime_type": "text/csv",
            "content_base64": base64.b64encode(GERMAN_CSV.encode("utf-8")).decode("ascii"),
        },
    )
    assert import_response.status_code == 201
    assert import_response.get_json()["report"]["imported_rows"] == 3

    list_response = client.get(
        "/api/v1/bank-transactions",
        query_string={"company_id": 1, "include_suggestions": "true"},
    )
    assert list_response.status_code == 200
    transactions = list_response.get_json()["transactions"]
    incoming = next(tx for tx in transactions if tx["amount"] == "1190.00")
    outgoing = next(tx for tx in transactions if tx["amount"] == "-595.00")
    assert incoming["suggestions"][0]["id"] == matching_entry_id

    match_response = client.post(
        f"/api/v1/bank-transactions/{incoming['id']}/match",
        json={"journal_entry_id": matching_entry_id},
    )
    assert match_response.status_code == 200
    assert match_response.get_json()["status"] == "matched"

    book_response = client.post(
        f"/api/v1/bank-transactions/{outgoing['id']}/book",
        json={"contra_account_id": rent_id, "description": "Miete Juli"},
    )
    assert book_response.status_code == 201
    booked = book_response.get_json()
    assert booked["status"] == "booked"
    assert booked["journal_entry_id"] is not None


def test_bank_transaction_reassign_via_api(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "4200", "name": "Miete", "account_type": "expense"},
    )
    target = client.post(
        "/api/v1/bank-accounts",
        json={"company_id": 1, "code": "1230", "name": "Finom Geschäftskonto"},
    ).get_json()

    with app.extensions["db_session_factory"]() as db_session:
        bank_id = db_session.execute(select(Account.id).where(Account.code == "1200")).scalar_one()
        rent_id = db_session.execute(select(Account.id).where(Account.code == "4200")).scalar_one()

    client.post(
        "/api/v1/bank-transactions/import",
        json={
            "company_id": 1,
            "bank_account_id": bank_id,
            "file_name": "umsaetze.csv",
            "mime_type": "text/csv",
            "content_base64": base64.b64encode(GERMAN_CSV.encode("utf-8")).decode("ascii"),
        },
    )
    transactions = client.get(
        "/api/v1/bank-transactions", query_string={"company_id": 1}
    ).get_json()["transactions"]
    single = transactions[0]

    # Ein Gegenkonto ist kein Bankkonto.
    rejected = client.post(
        f"/api/v1/bank-transactions/{single['id']}/bank-account",
        json={"bank_account_id": rent_id},
    )
    assert rejected.status_code == 422

    moved_single = client.post(
        f"/api/v1/bank-transactions/{single['id']}/bank-account",
        json={"bank_account_id": target["id"]},
    )
    assert moved_single.status_code == 200
    assert moved_single.get_json()["bank_account_id"] == target["id"]

    # Quellkonto und Einzel-IDs schließen sich aus.
    ambiguous = client.post(
        "/api/v1/bank-transactions/reassign",
        json={
            "company_id": 1,
            "target_bank_account_id": target["id"],
            "source_bank_account_id": bank_id,
            "transaction_ids": [single["id"]],
        },
    )
    assert ambiguous.status_code == 400

    moved_rest = client.post(
        "/api/v1/bank-transactions/reassign",
        json={
            "company_id": 1,
            "source_bank_account_id": bank_id,
            "target_bank_account_id": target["id"],
        },
    )
    assert moved_rest.status_code == 200
    assert moved_rest.get_json()["reassigned_count"] == 2

    remaining = client.get(
        "/api/v1/bank-transactions", query_string={"company_id": 1}
    ).get_json()["transactions"]
    assert {tx["bank_account_id"] for tx in remaining} == {target["id"]}


def test_bank_page_shows_and_changes_bank_account(tmp_path):
    app = _create_ui_app(tmp_path)
    client = app.test_client()
    client.post("/auth/login", data={"username": "admin", "password": "admin123"})
    client.post("/tenants", data={"tenant_name": "B Mandant", "company_name": "B GmbH"})
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1200", "name": "Bank", "account_type": "asset"},
    )
    client.post(
        "/accounts",
        data={"company_id": "1", "code": "1230", "name": "Finom", "account_type": "asset"},
    )
    client.post(
        "/bank/import",
        data={
            "company_id": "1",
            "bank_account_id": "1",
            "bank_csv": (BytesIO(GERMAN_CSV.encode("utf-8")), "umsaetze.csv"),
        },
        content_type="multipart/form-data",
    )

    page = client.get("/bank", query_string={"company_id": 1})
    assert "Umsätze auf anderes Bankkonto umhängen".encode() in page.data

    moved = client.post(
        "/bank/umhaengen",
        data={
            "company_id": "1",
            "source_bank_account_id": "1",
            "target_bank_account_id": "2",
        },
        follow_redirects=True,
    )
    assert moved.status_code == 200
    assert "3 Bankumsätze umgehängt".encode() in moved.data

    with app.extensions["db_session_factory"]() as db_session:
        accounts = set(
            db_session.execute(select(BankTransaction.bank_account_id)).scalars().all()
        )
    assert accounts == {2}

    back = client.post(
        "/bank/1/bankkonto",
        data={"company_id": "1", "bank_account_id": "1"},
        follow_redirects=True,
    )
    assert back.status_code == 200
    assert "umgehängt".encode() in back.data
