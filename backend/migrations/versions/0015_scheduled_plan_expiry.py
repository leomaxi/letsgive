"""add organizations.plan_starts_at / plan_expires_at, a scheduled admin
plan grant that auto-reverts to the Starter plan once it passes

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no backfill: NULL means "no scheduled expiry" -- a real,
    # meaningful value, same rationale as mailbox_connections.imap_last_uid
    # in migration 0013.
    op.add_column("organizations", sa.Column("plan_starts_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("plan_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "plan_expires_at")
    op.drop_column("organizations", "plan_starts_at")
