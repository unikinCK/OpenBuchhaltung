"""Protect finalized journal entries and audit logs at database level.

Revision ID: 20260713_0019
Revises: 20260713_0018
Create Date: 2026-07-13 14:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "20260713_0019"
down_revision = "20260713_0018"
branch_labels = None
depends_on = None


SQLITE_TRIGGER_NAMES = (
    "obk_journal_entry_finalized_no_update",
    "obk_journal_entry_finalized_no_delete",
    "obk_journal_entry_line_finalized_no_insert",
    "obk_journal_entry_line_finalized_no_update",
    "obk_journal_entry_line_finalized_no_delete",
    "obk_audit_log_no_update",
    "obk_audit_log_no_delete",
)


def _upgrade_sqlite() -> None:
    statements = (
        """
        CREATE TRIGGER obk_journal_entry_finalized_no_update
        BEFORE UPDATE ON journal_entry
        WHEN OLD.is_finalized = 1
        BEGIN
            SELECT RAISE(ABORT, 'finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_finalized_no_delete
        BEFORE DELETE ON journal_entry
        WHEN OLD.is_finalized = 1
        BEGIN
            SELECT RAISE(ABORT, 'finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_insert
        BEFORE INSERT ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id = NEW.journal_entry_id AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_update
        BEFORE UPDATE ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id IN (OLD.journal_entry_id, NEW.journal_entry_id)
              AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_delete
        BEFORE DELETE ON journal_entry_line
        WHEN EXISTS (
            SELECT 1 FROM journal_entry
            WHERE id = OLD.journal_entry_id AND is_finalized = 1
        )
        BEGIN
            SELECT RAISE(ABORT, 'lines of finalized journal entries are immutable');
        END
        """,
        """
        CREATE TRIGGER obk_audit_log_no_update
        BEFORE UPDATE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit logs are append-only');
        END
        """,
        """
        CREATE TRIGGER obk_audit_log_no_delete
        BEFORE DELETE ON audit_log
        BEGIN
            SELECT RAISE(ABORT, 'audit logs are append-only');
        END
        """,
    )
    for statement in statements:
        op.execute(sa.text(statement))


def _upgrade_postgresql() -> None:
    statements = (
        """
        CREATE FUNCTION obk_protect_finalized_journal_entry()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.is_finalized THEN
                RAISE EXCEPTION 'finalized journal entries are immutable'
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
        CREATE TRIGGER obk_journal_entry_finalized_no_update
        BEFORE UPDATE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry()
        """,
        """
        CREATE TRIGGER obk_journal_entry_finalized_no_delete
        BEFORE DELETE ON journal_entry
        FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry()
        """,
        """
        CREATE FUNCTION obk_protect_finalized_journal_entry_line()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            parent_is_finalized boolean;
        BEGIN
            -- The row locks serialize line mutations with the finalization update.
            -- Whichever operation obtains the parent lock first defines the order.
            IF TG_OP = 'INSERT' THEN
                SELECT is_finalized INTO parent_is_finalized
                FROM journal_entry
                WHERE id = NEW.journal_entry_id
                FOR SHARE;
            ELSIF TG_OP = 'DELETE' THEN
                SELECT is_finalized INTO parent_is_finalized
                FROM journal_entry
                WHERE id = OLD.journal_entry_id
                FOR SHARE;
            ELSE
                FOR parent_is_finalized IN
                    SELECT is_finalized
                    FROM journal_entry
                    WHERE id IN (OLD.journal_entry_id, NEW.journal_entry_id)
                    ORDER BY id
                    FOR SHARE
                LOOP
                    EXIT WHEN parent_is_finalized;
                END LOOP;
            END IF;

            IF parent_is_finalized THEN
                RAISE EXCEPTION 'lines of finalized journal entries are immutable'
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
        CREATE TRIGGER obk_journal_entry_line_finalized_no_insert
        BEFORE INSERT ON journal_entry_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry_line()
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_update
        BEFORE UPDATE ON journal_entry_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry_line()
        """,
        """
        CREATE TRIGGER obk_journal_entry_line_finalized_no_delete
        BEFORE DELETE ON journal_entry_line
        FOR EACH ROW EXECUTE FUNCTION obk_protect_finalized_journal_entry_line()
        """,
        """
        CREATE FUNCTION obk_protect_audit_log()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit logs are append-only'
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$
        """,
        """
        CREATE TRIGGER obk_audit_log_no_update
        BEFORE UPDATE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION obk_protect_audit_log()
        """,
        """
        CREATE TRIGGER obk_audit_log_no_delete
        BEFORE DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION obk_protect_audit_log()
        """,
    )
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _upgrade_sqlite()
    elif dialect == "postgresql":
        _upgrade_postgresql()
    else:
        raise RuntimeError(f"Unsupported database dialect for immutability triggers: {dialect}")


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for trigger_name in reversed(SQLITE_TRIGGER_NAMES):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    elif dialect == "postgresql":
        for trigger_name in (
            "obk_audit_log_no_delete",
            "obk_audit_log_no_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON audit_log"))
        for trigger_name in (
            "obk_journal_entry_line_finalized_no_delete",
            "obk_journal_entry_line_finalized_no_update",
            "obk_journal_entry_line_finalized_no_insert",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON journal_entry_line"))
        for trigger_name in (
            "obk_journal_entry_finalized_no_delete",
            "obk_journal_entry_finalized_no_update",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON journal_entry"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_audit_log()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_finalized_journal_entry_line()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS obk_protect_finalized_journal_entry()"))
    else:
        raise RuntimeError(f"Unsupported database dialect for immutability triggers: {dialect}")
