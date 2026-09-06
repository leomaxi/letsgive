"""widen organizations.nonprofit_type to hold a comma-joined multi-select list

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The frontend now lets an org select more than one nonprofit type (a plain
# HTML multi-select), stored here comma-joined -- String(100) was sized for
# a single free-text value and would truncate a handful of selections.
# batch_alter_table is used (not a bare op.alter_column) because SQLite has
# no native ALTER COLUMN and needs the recreate-table trick; it's a no-op
# wrapper on Postgres/MySQL, which both support a plain type change directly.
def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.alter_column(
            "nonprofit_type",
            existing_type=sa.String(length=100),
            type_=sa.String(length=500),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.alter_column(
            "nonprofit_type",
            existing_type=sa.String(length=500),
            type_=sa.String(length=100),
            existing_nullable=True,
        )
