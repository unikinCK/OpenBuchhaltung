"""Firmenstammdaten und Ausgangsrechnungen (ADR-006).

``company`` erhält Anschrift, Steuer-, Register- und Kontaktangaben, den Rechnungspräfix und das
Kleinunternehmer-Kennzeichen. Neu sind die Tabellen ``sales_invoice``, ``sales_invoice_line`` und
der Nummernkreis ``sales_invoice_sequence``. Trigger schützen ausgestellte Rechnungen auf
Datenbankebene: Erlaubt bleibt nur der Statuswechsel ``issued`` → ``cancelled``.

Revision ID: 20261103_0063
Revises: 20261103_0061
Create Date: 2026-11-03 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20261103_0063"
down_revision = "20261103_0061"
branch_labels = None
depends_on = None

COMPANY_TEXT_COLUMNS = (
    ("street", 255),
    ("postal_code", 20),
    ("city", 120),
    ("country_code", 2),
    ("vat_id", 20),
    ("tax_number", 30),
    ("phone", 50),
    ("email", 255),
    ("website", 255),
    ("register_court", 120),
    ("register_number", 50),
    ("managing_directors", 255),
    ("bank_name", 120),
)

# Spalten einer ausgestellten Rechnung, die sich beim Storno nicht ändern dürfen (alle bis auf
# ``status``).
IMMUTABLE_INVOICE_COLUMNS = (
    "tenant_id",
    "company_id",
    "partner_id",
    "invoice_number",
    "invoice_type",
    "cancels_invoice_id",
    "issue_date",
    "service_date",
    "service_period_from",
    "service_period_to",
    "due_date",
    "currency_code",
    "recipient_name",
    "recipient_street",
    "recipient_postal_code",
    "recipient_city",
    "recipient_country_code",
    "recipient_vat_id",
    "buyer_reference",
    "tax_treatment",
    "einvoice_syntax",
    "header_text",
    "footer_text",
    "net_total",
    "tax_total",
    "gross_total",
    "receivable_account_id",
    "journal_entry_id",
    "open_item_id",
    "pdf_document_id",
    "xml_document_id",
    "created_by",
    "created_at",
    "issued_by",
    "issued_at",
)

TRIGGER_NAMES = (
    "obk_sales_invoice_issued_no_update",
    "obk_sales_invoice_issued_no_delete",
    "obk_sales_invoice_line_issued_no_insert",
    "obk_sales_invoice_line_issued_no_update",
    "obk_sales_invoice_line_issued_no_delete",
)


def _upgrade_triggers_sqlite() -> None:
    unchanged = " AND ".join(
        f"NEW.{column} IS OLD.{column}" for column in IMMUTABLE_INVOICE_COLUMNS
    )
    statements = [
        f"""
        CREATE TRIGGER obk_sales_invoice_issued_no_update
        BEFORE UPDATE ON sales_invoice
        WHEN OLD.status <> 'draft' AND NOT (
            OLD.status = 'issued' AND NEW.status = 'cancelled' AND {unchanged}
        )
        BEGIN
            SELECT RAISE(ABORT, 'issued sales invoices are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_sales_invoice_issued_no_delete
        BEFORE DELETE ON sales_invoice
        WHEN OLD.status <> 'draft'
        BEGIN
            SELECT RAISE(ABORT, 'issued sales invoices are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_insert
        BEFORE INSERT ON sales_invoice_line
        WHEN EXISTS (
            SELECT 1 FROM sales_invoice
            WHERE id = NEW.sales_invoice_id AND status <> 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of issued sales invoices are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_update
        BEFORE UPDATE ON sales_invoice_line
        WHEN EXISTS (
            SELECT 1 FROM sales_invoice
            WHERE id IN (OLD.sales_invoice_id, NEW.sales_invoice_id) AND status <> 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of issued sales invoices are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_delete
        BEFORE DELETE ON sales_invoice_line
        WHEN EXISTS (
            SELECT 1 FROM sales_invoice
            WHERE id = OLD.sales_invoice_id AND status <> 'draft'
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of issued sales invoices are immutable');
        END
        """,
    ]
    for statement in statements:
        op.execute(sa.text(statement))


def _upgrade_triggers_postgresql() -> None:
    unchanged = " AND ".join(
        f"NEW.{column} IS NOT DISTINCT FROM OLD.{column}" for column in IMMUTABLE_INVOICE_COLUMNS
    )
    statements = [
        f"""
        CREATE FUNCTION obk_protect_issued_sales_invoice()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status <> 'draft' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'issued sales invoices are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                IF NOT (
                    OLD.status = 'issued' AND NEW.status = 'cancelled' AND {unchanged}
                ) THEN
                    RAISE EXCEPTION 'issued sales invoices are immutable'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE TRIGGER obk_sales_invoice_issued_no_update
        BEFORE UPDATE ON sales_invoice
        FOR EACH ROW EXECUTE FUNCTION obk_protect_issued_sales_invoice()
        """,
        """
        CREATE TRIGGER obk_sales_invoice_issued_no_delete
        BEFORE DELETE ON sales_invoice
        FOR EACH ROW EXECUTE FUNCTION obk_protect_issued_sales_invoice()
        """,
        """
        CREATE FUNCTION obk_protect_issued_sales_invoice_line()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_is_issued boolean;
        BEGIN
            -- FOR SHARE reiht Zeilenänderungen hinter das Ausstellen der Rechnung ein.
            IF TG_OP = 'INSERT' THEN
                SELECT status <> 'draft' INTO parent_is_issued
                FROM sales_invoice WHERE id = NEW.sales_invoice_id FOR SHARE;
            ELSIF TG_OP = 'DELETE' THEN
                SELECT status <> 'draft' INTO parent_is_issued
                FROM sales_invoice WHERE id = OLD.sales_invoice_id FOR SHARE;
            ELSE
                FOR parent_is_issued IN
                    SELECT status <> 'draft'
                    FROM sales_invoice
                    WHERE id IN (OLD.sales_invoice_id, NEW.sales_invoice_id)
                    ORDER BY id
                    FOR SHARE
                LOOP
                    EXIT WHEN parent_is_issued;
                END LOOP;
            END IF;

            IF parent_is_issued THEN
                RAISE EXCEPTION 'lines of issued sales invoices are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_insert
        BEFORE INSERT ON sales_invoice_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_issued_sales_invoice_line()
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_update
        BEFORE UPDATE ON sales_invoice_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_issued_sales_invoice_line()
        """,
        """
        CREATE TRIGGER obk_sales_invoice_line_issued_no_delete
        BEFORE DELETE ON sales_invoice_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_issued_sales_invoice_line()
        """,
    ]
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    for name, length in COMPANY_TEXT_COLUMNS:
        op.add_column("company", sa.Column(name, sa.String(length=length), nullable=True))
    op.add_column(
        "company",
        sa.Column("invoice_prefix", sa.String(length=10), nullable=False, server_default="RE"),
    )
    op.add_column(
        "company",
        sa.Column("is_small_business", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "sales_invoice_sequence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        sa.UniqueConstraint("company_id", "year", name="uq_sales_invoice_sequence_company_year"),
        sa.CheckConstraint("last_number >= 0", name="ck_sales_invoice_sequence_non_negative"),
    )

    op.create_table(
        "sales_invoice",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            sa.Integer(),
            sa.ForeignKey("company.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "partner_id",
            sa.Integer(),
            sa.ForeignKey("business_partner.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("invoice_number", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invoice_type", sa.String(length=20), nullable=False),
        sa.Column(
            "cancels_invoice_id",
            sa.Integer(),
            sa.ForeignKey("sales_invoice.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column("service_period_from", sa.Date(), nullable=True),
        sa.Column("service_period_to", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("currency_code", sa.String(length=3), nullable=False),
        sa.Column("recipient_name", sa.String(length=255), nullable=True),
        sa.Column("recipient_street", sa.String(length=255), nullable=True),
        sa.Column("recipient_postal_code", sa.String(length=20), nullable=True),
        sa.Column("recipient_city", sa.String(length=120), nullable=True),
        sa.Column("recipient_country_code", sa.String(length=2), nullable=True),
        sa.Column("recipient_vat_id", sa.String(length=20), nullable=True),
        sa.Column("buyer_reference", sa.String(length=100), nullable=True),
        sa.Column("tax_treatment", sa.String(length=20), nullable=False),
        sa.Column("einvoice_syntax", sa.String(length=3), nullable=False),
        sa.Column("header_text", sa.Text(), nullable=True),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("net_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("gross_total", sa.Numeric(14, 2), nullable=False),
        sa.Column(
            "receivable_account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "journal_entry_id",
            sa.Integer(),
            sa.ForeignKey("journal_entry.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "open_item_id",
            sa.Integer(),
            sa.ForeignKey("open_item.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "pdf_document_id",
            sa.Integer(),
            sa.ForeignKey("document.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "xml_document_id",
            sa.Integer(),
            sa.ForeignKey("document.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by", sa.String(length=120), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("company_id", "invoice_number", name="uq_sales_invoice_company_number"),
        sa.UniqueConstraint("cancels_invoice_id", name="uq_sales_invoice_cancels"),
        sa.CheckConstraint(
            "status IN ('draft', 'issued', 'cancelled')", name="ck_sales_invoice_status"
        ),
        sa.CheckConstraint(
            "invoice_type IN ('invoice', 'cancellation')", name="ck_sales_invoice_type"
        ),
        sa.CheckConstraint(
            "tax_treatment IN ('standard', 'reverse_charge', 'intra_community', 'export', "
            "'exempt', 'small_business')",
            name="ck_sales_invoice_tax_treatment",
        ),
        sa.CheckConstraint("einvoice_syntax IN ('ubl', 'cii')", name="ck_sales_invoice_syntax"),
        sa.CheckConstraint(
            "status = 'draft' OR invoice_number IS NOT NULL", name="ck_sales_invoice_number_set"
        ),
    )
    op.create_index("ix_sales_invoice_company_status", "sales_invoice", ["company_id", "status"])
    op.create_index("ix_sales_invoice_partner", "sales_invoice", ["partner_id"])

    op.create_table(
        "sales_invoice_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenant.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sales_invoice_id",
            sa.Integer(),
            sa.ForeignKey("sales_invoice.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 4), nullable=False),
        sa.Column("unit_code", sa.String(length=3), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "tax_code_id",
            sa.Integer(),
            sa.ForeignKey("tax_code.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "revenue_account_id",
            sa.Integer(),
            sa.ForeignKey("account.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("tax_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 2), nullable=False),
        sa.UniqueConstraint("sales_invoice_id", "line_number", name="uq_sales_invoice_line_number"),
        sa.CheckConstraint("tax_rate >= 0", name="ck_sales_invoice_line_tax_rate"),
        sa.CheckConstraint("unit_price >= 0", name="ck_sales_invoice_line_unit_price"),
    )

    if op.get_bind().dialect.name == "sqlite":
        _upgrade_triggers_sqlite()
    else:
        _upgrade_triggers_postgresql()


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for trigger_name in reversed(TRIGGER_NAMES):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    else:
        for trigger_name in reversed(TRIGGER_NAMES[2:]):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON sales_invoice_line"))
        for trigger_name in reversed(TRIGGER_NAMES[:2]):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON sales_invoice"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_issued_sales_invoice_line()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_issued_sales_invoice()"))

    op.drop_table("sales_invoice_line")
    op.drop_index("ix_sales_invoice_partner", table_name="sales_invoice")
    op.drop_index("ix_sales_invoice_company_status", table_name="sales_invoice")
    op.drop_table("sales_invoice")
    op.drop_table("sales_invoice_sequence")

    op.drop_column("company", "is_small_business")
    op.drop_column("company", "invoice_prefix")
    for name, _length in reversed(COMPANY_TEXT_COLUMNS):
        op.drop_column("company", name)
