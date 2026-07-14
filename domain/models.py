from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all persistence entities."""


class Tenant(Base):
    __tablename__ = "tenant"
    __table_args__ = (
        CheckConstraint(
            "audit_sequence_number >= 0", name="ck_tenant_audit_sequence_non_negative"
        ),
        CheckConstraint(
            "length(audit_head_hash) = 64", name="ck_tenant_audit_head_hash_length"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    audit_sequence_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    audit_head_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0" * 64, server_default="0" * 64
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    companies: Mapped[list[Company]] = relationship(back_populates="tenant", cascade="all, delete")


class Company(Base):
    __tablename__ = "company"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_company_tenant_name"),
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="ck_company_fiscal_year_start_month",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    # Erster Monat des Wirtschaftsjahres (1 = Januar = Kalenderjahr, sonst abweichendes WJ).
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    tenant: Mapped[Tenant] = relationship(back_populates="companies")
    fiscal_years: Mapped[list[FiscalYear]] = relationship(
        back_populates="company", cascade="all, delete"
    )
    accounts: Mapped[list[Account]] = relationship(back_populates="company", cascade="all, delete")
    tax_codes: Mapped[list[TaxCode]] = relationship(back_populates="company", cascade="all, delete")
    journal_entries: Mapped[list[JournalEntry]] = relationship(
        back_populates="company", cascade="all, delete"
    )
    payroll_employees: Mapped[list[PayrollEmployee]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    payroll_runs: Mapped[list[PayrollRun]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    income_tax_returns: Mapped[list[IncomeTaxReturn]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    controlling_units: Mapped[list[ControllingUnit]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )


class FiscalYear(Base):
    __tablename__ = "fiscal_year"
    __table_args__ = (
        UniqueConstraint("company_id", "label", name="uq_fiscal_year_company_label"),
        CheckConstraint("start_date < end_date", name="ck_fiscal_year_date_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    company: Mapped[Company] = relationship(back_populates="fiscal_years")
    periods: Mapped[list[Period]] = relationship(
        back_populates="fiscal_year", cascade="all, delete"
    )
    journal_entries: Mapped[list[JournalEntry]] = relationship(
        back_populates="fiscal_year", cascade="all, delete"
    )
    income_tax_returns: Mapped[list[IncomeTaxReturn]] = relationship(
        back_populates="fiscal_year"
    )


class Period(Base):
    __tablename__ = "period"
    __table_args__ = (
        UniqueConstraint("fiscal_year_id", "period_number", name="uq_period_fiscal_year_number"),
        CheckConstraint("period_number BETWEEN 1 AND 13", name="ck_period_number_range"),
        CheckConstraint("start_date <= end_date", name="ck_period_date_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year_id: Mapped[int] = mapped_column(
        ForeignKey("fiscal_year.id", ondelete="CASCADE"), nullable=False
    )
    period_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # Abschlussperiode (Periode 13) fuer Jahresabschlussbuchungen; nicht datumsbasiert bebucht.
    is_closing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    fiscal_year: Mapped[FiscalYear] = relationship(back_populates="periods")
    locks: Mapped[list[PeriodLock]] = relationship(back_populates="period", cascade="all, delete")
    journal_entries: Mapped[list[JournalEntry]] = relationship(
        back_populates="period", cascade="all, delete"
    )


class PeriodLock(Base):
    __tablename__ = "period_lock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    period_id: Mapped[int] = mapped_column(
        ForeignKey("period.id", ondelete="CASCADE"), nullable=False
    )
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    reason: Mapped[str | None] = mapped_column(String(255))
    locked_by: Mapped[str] = mapped_column(String(120), nullable=False)

    period: Mapped[Period] = relationship(back_populates="locks")


class Account(Base):
    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_account_company_code"),
        CheckConstraint("hierarchy_level BETWEEN 1 AND 4", name="ck_account_hierarchy_level_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[str] = mapped_column(String(30), nullable=False)
    hierarchy_level: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    level_1: Mapped[str] = mapped_column(String(1), nullable=False, default="0")
    level_2: Mapped[str] = mapped_column(String(1), nullable=False, default="0")
    level_3: Mapped[str] = mapped_column(String(1), nullable=False, default="0")
    level_4: Mapped[str] = mapped_column(String(1), nullable=False, default="0")
    parent_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("account.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="accounts")
    journal_lines: Mapped[list[JournalEntryLine]] = relationship(back_populates="account")
    parent_account: Mapped[Account | None] = relationship(
        remote_side="Account.id", back_populates="child_accounts"
    )
    child_accounts: Mapped[list[Account]] = relationship(back_populates="parent_account")

    @validates("code")
    def _sync_hierarchy(self, key: str, value: str) -> str:
        del key
        normalized_code = (value or "").strip()
        self.level_1, self.level_2, self.level_3, self.level_4, self.hierarchy_level = (
            self.derive_hierarchy(normalized_code)
        )
        return normalized_code

    @staticmethod
    def derive_hierarchy(code: str) -> tuple[str, str, str, str, int]:
        digits = "".join(char for char in code if char.isdigit())
        padded = (digits[:4]).ljust(4, "0")
        if padded[3] != "0":
            level = 4
        elif padded[2] != "0":
            level = 3
        elif padded[1] != "0":
            level = 2
        else:
            level = 1
        return padded[0], padded[1], padded[2], padded[3], level


class ControllingUnit(Base):
    __tablename__ = "controlling_unit"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "unit_type",
            "code",
            name="uq_controlling_unit_company_type_code",
        ),
        CheckConstraint(
            "unit_type IN ('cost_center', 'profit_center')",
            name="ck_controlling_unit_type",
        ),
        CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to",
            name="ck_controlling_unit_validity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    unit_type: Mapped[str] = mapped_column(String(30), nullable=False)
    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="SET NULL")
    )
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    company: Mapped[Company] = relationship(back_populates="controlling_units")
    parent: Mapped[ControllingUnit | None] = relationship(
        remote_side="ControllingUnit.id", back_populates="children"
    )
    children: Mapped[list[ControllingUnit]] = relationship(back_populates="parent")
    cost_center_lines: Mapped[list[JournalEntryLine]] = relationship(
        foreign_keys="JournalEntryLine.cost_center_id", back_populates="cost_center"
    )
    profit_center_lines: Mapped[list[JournalEntryLine]] = relationship(
        foreign_keys="JournalEntryLine.profit_center_id", back_populates="profit_center"
    )


class TaxCode(Base):
    __tablename__ = "tax_code"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_tax_code_company_code"),
        CheckConstraint("rate >= 0", name="ck_tax_code_rate_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(20), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    vat_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="tax_codes")
    vat_account: Mapped[Account | None] = relationship(foreign_keys=[vat_account_id])
    journal_lines: Mapped[list[JournalEntryLine]] = relationship(back_populates="tax_code")


class JournalEntry(Base):
    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint("company_id", "posting_number", name="uq_journal_entry_company_no"),
        # Eine Buchung darf höchstens einmal storniert werden.
        UniqueConstraint("reversal_of_id", name="uq_journal_entry_reversal_of"),
        CheckConstraint(
            "((is_finalized = false AND content_hash IS NULL "
            "AND content_hash_version IS NULL) OR "
            "(is_finalized = true AND content_hash IS NOT NULL "
            "AND length(content_hash) = 64 AND content_hash_version IS NOT NULL "
            "AND content_hash_version = 2))",
            name="ck_journal_entry_finalized_content_hash",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year_id: Mapped[int] = mapped_column(
        ForeignKey("fiscal_year.id", ondelete="RESTRICT"), nullable=False
    )
    period_id: Mapped[int] = mapped_column(
        ForeignKey("period.id", ondelete="RESTRICT"), nullable=False
    )
    posting_number: Mapped[str] = mapped_column(String(30), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    # GoBD-Festschreibung: festgeschriebene Buchungen sind unveränderbar;
    # Korrekturen erfolgen ausschließlich über Stornobuchungen.
    is_finalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_by: Mapped[str | None] = mapped_column(String(120))
    content_hash_version: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    # Gesetzt auf der Stornobuchung: verweist auf die stornierte Originalbuchung.
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    company: Mapped[Company] = relationship(back_populates="journal_entries")
    fiscal_year: Mapped[FiscalYear] = relationship(back_populates="journal_entries")
    period: Mapped[Period] = relationship(back_populates="journal_entries")
    lines: Mapped[list[JournalEntryLine]] = relationship(
        back_populates="journal_entry", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(back_populates="journal_entry")
    reversal_of: Mapped[JournalEntry | None] = relationship(
        remote_side="JournalEntry.id", back_populates="reversed_by"
    )
    reversed_by: Mapped[JournalEntry | None] = relationship(
        back_populates="reversal_of", uselist=False
    )


class JournalEntryLine(Base):
    __tablename__ = "journal_entry_line"
    __table_args__ = (
        UniqueConstraint("journal_entry_id", "line_number", name="uq_je_line_number"),
        CheckConstraint("debit_amount >= 0", name="ck_je_line_debit_non_negative"),
        CheckConstraint("credit_amount >= 0", name="ck_je_line_credit_non_negative"),
        CheckConstraint(
            "((debit_amount = 0 AND credit_amount > 0) OR "
            "(credit_amount = 0 AND debit_amount > 0))",
            name="ck_je_line_single_side",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    journal_entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_code.id", ondelete="RESTRICT"))
    cost_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    profit_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    description: Mapped[str | None] = mapped_column(String(255))
    debit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    credit_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")

    journal_entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship(back_populates="journal_lines")
    tax_code: Mapped[TaxCode | None] = relationship(back_populates="journal_lines")
    cost_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[cost_center_id], back_populates="cost_center_lines"
    )
    profit_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[profit_center_id], back_populates="profit_center_lines"
    )

    @validates("debit_amount", "credit_amount")
    def validate_debit_credit(self, key: str, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError(f"{key} must be non-negative")

        debit = value if key == "debit_amount" else self.debit_amount or Decimal("0.00")
        credit = value if key == "credit_amount" else self.credit_amount or Decimal("0.00")

        if debit > 0 and credit > 0:
            raise ValueError("Debit and credit cannot both be greater than zero")

        if debit == 0 and credit == 0:
            raise ValueError("Either debit or credit must be greater than zero")

        return value


class Document(Base):
    __tablename__ = "document"
    __table_args__ = (
        UniqueConstraint("tenant_id", "storage_key", name="uq_document_tenant_storage_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="SET NULL")
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    replaces_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.id", ondelete="SET NULL")
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_date: Mapped[date | None] = mapped_column(Date)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    journal_entry: Mapped[JournalEntry | None] = relationship(back_populates="documents")
    replaces_document: Mapped[Document | None] = relationship(remote_side="Document.id")


class BankTransaction(Base):
    __tablename__ = "bank_transaction"
    __table_args__ = (
        UniqueConstraint("company_id", "dedup_hash", name="uq_bank_tx_company_hash"),
        CheckConstraint("amount != 0", name="ck_bank_tx_amount_non_zero"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    bank_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    booking_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Signiert: positiv = Zahlungseingang, negativ = Zahlungsausgang
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    purpose: Mapped[str] = mapped_column(String(255), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(255))
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="SET NULL")
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    bank_account: Mapped[Account] = relationship(foreign_keys=[bank_account_id])
    journal_entry: Mapped[JournalEntry | None] = relationship()


class OpenItem(Base):
    __tablename__ = "open_item"
    __table_args__ = (
        CheckConstraint("original_amount > 0", name="ck_open_item_original_amount_positive"),
        CheckConstraint("open_amount >= 0", name="ck_open_item_open_amount_non_negative"),
        CheckConstraint(
            "item_type IN ('receivable', 'payable')", name="ck_open_item_type_known"
        ),
        CheckConstraint("status IN ('open', 'settled')", name="ck_open_item_status_known"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="SET NULL")
    )
    bank_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_transaction.id", ondelete="SET NULL")
    )
    item_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str] = mapped_column(String(120), nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(255))
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    open_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_by: Mapped[str | None] = mapped_column(String(120))

    account: Mapped[Account] = relationship()
    journal_entry: Mapped[JournalEntry | None] = relationship()
    bank_transaction: Mapped[BankTransaction | None] = relationship()


class FixedAsset(Base):
    """Anlagegut der Anlagenbuchhaltung (Sachanlage) inkl. AfA-Parametern."""

    __tablename__ = "fixed_asset"
    __table_args__ = (
        UniqueConstraint("company_id", "asset_number", name="uq_fixed_asset_company_number"),
        CheckConstraint("acquisition_cost > 0", name="ck_fixed_asset_cost_positive"),
        CheckConstraint("residual_value >= 0", name="ck_fixed_asset_residual_non_negative"),
        CheckConstraint(
            "method IN ('linear', 'degressive', 'leistung', 'gwg', 'sammelposten', 'manuell')",
            name="ck_fixed_asset_method_known",
        ),
        CheckConstraint(
            "status IN ('active', 'disposed', 'fully_depreciated')",
            name="ck_fixed_asset_status_known",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    asset_number: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    acquisition_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Datum der Inbetriebnahme = Beginn der AfA (§ 7 Abs. 1 Satz 4 EStG).
    in_service_date: Mapped[date] = mapped_column(Date, nullable=False)
    acquisition_cost: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    useful_life_months: Mapped[int | None] = mapped_column(Integer)
    degressive_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    total_units: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    residual_value: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0.00")
    )
    keep_memo_value: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Bilanzkonto (Sachanlage) und Aufwandskonto (Abschreibungen).
    asset_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    depreciation_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    cost_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    profit_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    disposal_date: Mapped[date | None] = mapped_column(Date)
    disposal_proceeds: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    company: Mapped[Company] = relationship()
    asset_account: Mapped[Account] = relationship(foreign_keys=[asset_account_id])
    depreciation_account: Mapped[Account] = relationship(foreign_keys=[depreciation_account_id])
    cost_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[cost_center_id]
    )
    profit_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[profit_center_id]
    )
    depreciation_entries: Mapped[list[DepreciationEntry]] = relationship(
        back_populates="fixed_asset", cascade="all, delete-orphan"
    )


class DepreciationEntry(Base):
    """Verbuchte Abschreibungszeile eines Anlageguts für ein Wirtschaftsjahr."""

    __tablename__ = "depreciation_entry"
    __table_args__ = (
        UniqueConstraint(
            "fixed_asset_id", "fiscal_year", "kind", name="uq_depreciation_asset_year_kind"
        ),
        CheckConstraint("amount > 0", name="ck_depreciation_amount_positive"),
        CheckConstraint(
            "kind IN ('planmaessig', 'ausserplanmaessig', 'abgang')",
            name="ck_depreciation_kind_known",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    fixed_asset_id: Mapped[int] = mapped_column(
        ForeignKey("fixed_asset.id", ondelete="CASCADE"), nullable=False
    )
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="SET NULL")
    )
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    depreciation_date: Mapped[date] = mapped_column(Date, nullable=False)
    # planmaessig (laut Plan), ausserplanmaessig (AfaA/§ 253 Abs. 3 HGB), abgang (Restbuchwert).
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="planmaessig")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    book_value_before: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    book_value_after: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    fixed_asset: Mapped[FixedAsset] = relationship(back_populates="depreciation_entries")
    journal_entry: Mapped[JournalEntry | None] = relationship()


class VatReturn(Base):
    """Festgehaltene Umsatzsteuer-Voranmeldung (Kennziffern-Snapshot je Zeitraum)."""

    __tablename__ = "vat_return"
    __table_args__ = (
        UniqueConstraint("company_id", "period_label", name="uq_vat_return_company_period"),
        CheckConstraint("date_from <= date_to", name="ck_vat_return_date_range"),
        CheckConstraint(
            "status IN ('erstellt', 'uebermittelt')", name="ck_vat_return_status_known"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    # Voranmeldungszeitraum, z. B. "2026-05" (Monat) oder "2026-Q2" (Quartal).
    period_label: Mapped[str] = mapped_column(String(10), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    # Kennziffern-Snapshot: Liste von {kennziffer, label, amount} zum Zeitpunkt des Festhaltens.
    kennzahlen: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="erstellt")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)

    company: Mapped[Company] = relationship()
    elster_submissions: Mapped[list[ElsterSubmission]] = relationship(
        back_populates="vat_return", cascade="all, delete-orphan"
    )


class ElsterSubmission(Base):
    """Protokollierte ELSTER-Übermittlung bzw. Testübermittlung."""

    __tablename__ = "elster_submission"
    __table_args__ = (
        CheckConstraint(
            "procedure IN ('ustva', 'ust_jahreserklaerung')",
            name="ck_elster_submission_procedure_known",
        ),
        CheckConstraint(
            "environment IN ('test', 'production')",
            name="ck_elster_submission_environment_known",
        ),
        CheckConstraint(
            "status IN ('created', 'transmitted', 'failed')",
            name="ck_elster_submission_status_known",
        ),
        CheckConstraint(
            "transport IN ('mock', 'eric')", name="ck_elster_submission_transport_known"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    vat_return_id: Mapped[int] = mapped_column(
        ForeignKey("vat_return.id", ondelete="CASCADE"), nullable=False
    )
    procedure: Mapped[str] = mapped_column(String(20), nullable=False, default="ustva")
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="test")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")
    transport: Mapped[str] = mapped_column(String(20), nullable=False, default="mock")
    certificate_alias: Mapped[str | None] = mapped_column(String(120))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_xml: Mapped[str] = mapped_column(Text, nullable=False)
    response_protocol: Mapped[str | None] = mapped_column(Text)
    transfer_ticket: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)

    company: Mapped[Company] = relationship()
    vat_return: Mapped[VatReturn] = relationship(back_populates="elster_submissions")


class IncomeTaxReturn(Base):
    """Festgehaltener KSt-/GewSt-Snapshot fuer Erklaerung oder Vorauszahlung."""

    __tablename__ = "income_tax_return"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "tax_type",
            "declaration_type",
            "period_label",
            name="uq_income_tax_return_scope",
        ),
        CheckConstraint(
            "tax_type IN ('corporate_income', 'trade_tax')",
            name="ck_income_tax_return_tax_type_known",
        ),
        CheckConstraint(
            "declaration_type IN ('declaration', 'prepayment_adjustment')",
            name="ck_income_tax_return_declaration_type_known",
        ),
        CheckConstraint(
            "status IN ('erstellt', 'uebermittelt')",
            name="ck_income_tax_return_status_known",
        ),
        CheckConstraint("date_from <= date_to", name="ck_income_tax_return_date_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    fiscal_year_id: Mapped[int | None] = mapped_column(
        ForeignKey("fiscal_year.id", ondelete="RESTRICT")
    )
    tax_type: Mapped[str] = mapped_column(String(30), nullable=False)
    declaration_type: Mapped[str] = mapped_column(String(30), nullable=False)
    period_label: Mapped[str] = mapped_column(String(20), nullable=False)
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="erstellt")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)

    company: Mapped[Company] = relationship(back_populates="income_tax_returns")
    fiscal_year: Mapped[FiscalYear | None] = relationship(
        back_populates="income_tax_returns"
    )


class PayrollEmployee(Base):
    """Mitarbeiter-Stammdaten fuer das Lohnbuchhaltungs-MVP."""

    __tablename__ = "payroll_employee"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_number", name="uq_payroll_employee_number"),
        CheckConstraint(
            "status IN ('active', 'inactive')", name="ck_payroll_employee_status_known"
        ),
        CheckConstraint("gross_monthly_salary >= 0", name="ck_payroll_employee_salary_nonneg"),
        CheckConstraint("wage_tax_rate >= 0", name="ck_payroll_employee_wage_tax_nonneg"),
        CheckConstraint("tax_class BETWEEN 1 AND 6", name="ck_payroll_employee_tax_class"),
        CheckConstraint(
            "employee_social_security_rate >= 0",
            name="ck_payroll_employee_employee_sv_nonneg",
        ),
        CheckConstraint(
            "employer_social_security_rate >= 0",
            name="ck_payroll_employee_employer_sv_nonneg",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    employee_number: Mapped[str] = mapped_column(String(40), nullable=False)
    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    employment_start: Mapped[date] = mapped_column(Date, nullable=False)
    employment_end: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    birth_date: Mapped[date | None] = mapped_column(Date)
    tax_id: Mapped[str | None] = mapped_column(String(40))
    tax_class: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    child_allowances: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, default=0)
    federal_state: Mapped[str | None] = mapped_column(String(2))
    main_employment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    social_security_number: Mapped[str | None] = mapped_column(String(40))
    gross_monthly_salary: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    wage_tax_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False, default=0)
    church_tax_rate: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False, default=0)
    solidarity_surcharge_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=0
    )
    employee_social_security_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=0
    )
    employer_social_security_rate: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=0
    )
    wage_expense_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    employer_social_security_expense_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    payroll_liability_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    wage_tax_liability_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    social_security_liability_account_id: Mapped[int] = mapped_column(
        ForeignKey("account.id", ondelete="RESTRICT"), nullable=False
    )
    cost_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    profit_center_id: Mapped[int | None] = mapped_column(
        ForeignKey("controlling_unit.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    company: Mapped[Company] = relationship(back_populates="payroll_employees")
    wage_expense_account: Mapped[Account] = relationship(foreign_keys=[wage_expense_account_id])
    employer_social_security_expense_account: Mapped[Account] = relationship(
        foreign_keys=[employer_social_security_expense_account_id]
    )
    payroll_liability_account: Mapped[Account] = relationship(
        foreign_keys=[payroll_liability_account_id]
    )
    wage_tax_liability_account: Mapped[Account] = relationship(
        foreign_keys=[wage_tax_liability_account_id]
    )
    social_security_liability_account: Mapped[Account] = relationship(
        foreign_keys=[social_security_liability_account_id]
    )
    cost_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[cost_center_id]
    )
    profit_center: Mapped[ControllingUnit | None] = relationship(
        foreign_keys=[profit_center_id]
    )
    payroll_lines: Mapped[list[PayrollRunLine]] = relationship(back_populates="employee")


class PayrollRun(Base):
    """Lohnlauf je Gesellschaft und Meldezeitraum."""

    __tablename__ = "payroll_run"
    __table_args__ = (
        UniqueConstraint("company_id", "period_label", name="uq_payroll_run_company_period"),
        CheckConstraint("status IN ('draft', 'posted')", name="ck_payroll_run_status_known"),
        CheckConstraint("gross_total >= 0", name="ck_payroll_run_gross_nonneg"),
        CheckConstraint("net_total >= 0", name="ck_payroll_run_net_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entry.id", ondelete="SET NULL")
    )
    period_label: Mapped[str] = mapped_column(String(10), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    gross_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    wage_tax_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    church_tax_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    solidarity_surcharge_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    employee_social_security_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    employer_social_security_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    net_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    company: Mapped[Company] = relationship(back_populates="payroll_runs")
    journal_entry: Mapped[JournalEntry | None] = relationship()
    lines: Mapped[list[PayrollRunLine]] = relationship(
        back_populates="payroll_run", cascade="all, delete-orphan"
    )


class PayrollRunLine(Base):
    """Mitarbeiterzeile eines Lohnlaufs."""

    __tablename__ = "payroll_run_line"
    __table_args__ = (
        UniqueConstraint("payroll_run_id", "employee_id", name="uq_payroll_line_employee"),
        CheckConstraint("gross_pay >= 0", name="ck_payroll_line_gross_nonneg"),
        CheckConstraint("net_pay >= 0", name="ck_payroll_line_net_nonneg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), nullable=False
    )
    payroll_run_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_run.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_employee.id", ondelete="RESTRICT"), nullable=False
    )
    gross_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    wage_tax: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    church_tax: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    solidarity_surcharge: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    employee_social_security: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    employer_social_security: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    net_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    employer_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    payroll_run: Mapped[PayrollRun] = relationship(back_populates="lines")
    employee: Mapped[PayrollEmployee] = relationship(back_populates="payroll_lines")


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    api_token_hash: Mapped[str | None] = mapped_column(String(255), unique=True)
    api_token_last4: Mapped[str | None] = mapped_column(String(4))
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="Buchhalter")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    tenant: Mapped[Tenant | None] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "sequence_number", name="uq_audit_log_tenant_sequence"
        ),
        UniqueConstraint(
            "tenant_id", "previous_hash", name="uq_audit_log_tenant_previous_hash"
        ),
        CheckConstraint("sequence_number > 0", name="ck_audit_log_sequence_positive"),
        CheckConstraint("hash_version = 1", name="ck_audit_log_hash_version"),
        CheckConstraint(
            "length(previous_hash) = 64", name="ck_audit_log_previous_hash_length"
        ),
        CheckConstraint("length(entry_hash) = 64", name="ck_audit_log_entry_hash_length"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    hash_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
