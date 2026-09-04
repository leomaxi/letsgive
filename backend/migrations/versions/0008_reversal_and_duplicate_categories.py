"""add reversed/duplicate reconciliation categories

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-03

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLite (dev/test) stores these as plain TEXT with no native enum type to
# alter -- a new Python-side enum member is usable immediately, no schema
# change needed. Postgres enforces a real CREATE TYPE ... AS ENUM, so it
# needs an explicit ALTER TYPE ... ADD VALUE per new label or every insert
# of the new value would be rejected at the database level.
def upgrade() -> None:
    bind = op.get_bind()
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
    # recreating the type (and reassigning every column using it) -- not
    # worth doing for a purely additive change with no rows expected to
    # exist in the removed states at downgrade time. No-op by design.
    pass
