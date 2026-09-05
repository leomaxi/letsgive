"""add reversed/duplicate reconciliation categories

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite (dev/test) stores these as plain TEXT with no native enum type to
# alter -- a new Python-side enum member is usable immediately, no schema
# change needed. Postgres and MySQL both enforce a real native enum type at
# the column level, so each needs its own dialect-specific way to add the
# two new labels or every insert of a new value would be rejected by the
# database itself, not just by the ORM.
def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "mysql":
        # MySQL has no separate named enum type (unlike Postgres) -- the
        # allowed values live inline on the column, so widening them is a
        # single MODIFY COLUMN restating the full new value list, not an
        # ADD VALUE against a type.
        op.alter_column(
            "contribution_events",
            "decision",
            existing_type=sa.Enum(
                "accepted",
                "ambiguous",
                "excluded_time_window",
                "excluded_source_mismatch",
                "excluded_no_credit_intent",
                "excluded_no_active_session",
                name="contribution_decision",
            ),
            type_=sa.Enum(
                "accepted",
                "ambiguous",
                "excluded_time_window",
                "excluded_source_mismatch",
                "excluded_no_credit_intent",
                "excluded_no_active_session",
                "reversed",
                name="contribution_decision",
            ),
            existing_nullable=False,
        )
        op.alter_column(
            "reconciliation_items",
            "resolution",
            existing_type=sa.Enum("accepted", "excluded", name="reconciliation_resolution"),
            type_=sa.Enum(
                "accepted", "excluded", "reversed", "duplicate", name="reconciliation_resolution"
            ),
            existing_nullable=True,
        )
        return

    if bind.dialect.name != "postgresql":
        return

    # Each ADD VALUE must run in its own statement/autocommit context on
    # Postgres < 12 (can't add multiple values or run inside the same
    # transaction as a subsequent use of that value); IF NOT EXISTS (PG 12+)
    # keeps this safe to re-run.
    op.execute("ALTER TYPE contribution_decision ADD VALUE IF NOT EXISTS 'reversed'")
    op.execute("ALTER TYPE reconciliation_resolution ADD VALUE IF NOT EXISTS 'reversed'")
    op.execute("ALTER TYPE reconciliation_resolution ADD VALUE IF NOT EXISTS 'duplicate'")


def downgrade() -> None:
    # Postgres has no supported way to drop a single enum label short of
    # recreating the type (and reassigning every column using it). MySQL
    # could shrink the inline value list back down, but only safely if no
    # row already uses 'reversed'/'duplicate' -- not worth the risk for a
    # purely additive change with no rows expected in the removed states at
    # downgrade time. No-op by design on every dialect.
    pass
