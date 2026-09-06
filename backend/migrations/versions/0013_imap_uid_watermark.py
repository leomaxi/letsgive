"""add mailbox_connections.imap_last_uid, a real UID watermark for IMAP
polling (replaces relying on the mailbox's own \\Seen flag)

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, no backfill: NULL is a real, meaningful value here (see the
    # model's docstring) -- a connection that predates this column simply
    # does one full catch-up sweep on its next poll instead of skipping
    # everything already sitting in the mailbox.
    op.add_column("mailbox_connections", sa.Column("imap_last_uid", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("mailbox_connections", "imap_last_uid")
