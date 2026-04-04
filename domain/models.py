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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    companies: Mapped[list[Company]] = relationship(back_populates="tenant", cascade="all, delete")


class Company(Base):
    __tablename__ = "company"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_company_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
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
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_account_company_code"),)

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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="accounts")
    journal_lines: Mapped[list[JournalEntryLine]] = relationship(back_populates="account")


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
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="tax_codes")
    journal_lines: Mapped[list[JournalEntryLine]] = relationship(back_populates="tax_code")


class JournalEntry(Base):
    __tablename__ = "journal_entry"
    __table_args__ = (
        UniqueConstraint("company_id", "posting_number", name="uq_journal_entry_company_no"),
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
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    journal_entry: Mapped[JournalEntry | None] = relationship(back_populates="documents")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int | None] = mapped_column(ForeignKey("company.id", ondelete="SET NULL"))
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    changed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
