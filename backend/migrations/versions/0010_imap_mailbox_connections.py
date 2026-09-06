"""add IMAP ("log in with your email") fields to mailbox_connections

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plain nullable ADD COLUMN, no FK/constraint involved -- safe unbatched
    # on SQLite too (batch mode is only required here for the webhook_health
    # width change below).
    op.add_column("mailbox_connections", sa.Column("imap_host", sa.String(length=255)))
    op.add_column("mailbox_connections", sa.Column("imap_port", sa.Integer()))
    op.add_column(
        "mailbox_connections", sa.Column("imap_password", sa.String(length=500))
    )
    with op.batch_alter_table("mailbox_connections") as batch_op:
        batch_op.alter_column(
            "webhook_health",
            existing_type=sa.String(length=50),
            type_=sa.String(length=255),
            existing_nullable=True,
        )

    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.alter_column(
            "mailbox_connections",
            "provider",
            existing_type=sa.Enum("fake", "microsoft", "gmail", name="mailbox_provider"),
            type_=sa.Enum("fake", "microsoft", "gmail", "imap", name="mailbox_provider"),
            existing_nullable=False,
        )
    elif bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE mailbox_provider ADD VALUE IF NOT EXISTS 'imap'")


def downgrade() -> None:
    # See migration 0008 for why shrinking a Postgres enum type back down
    # isn't attempted here -- no supported way short of recreating the type.
    with op.batch_alter_table("mailbox_connections") as batch_op:
        batch_op.alter_column(
            "webhook_health",
            existing_type=sa.String(length=255),
            type_=sa.String(length=50),
            existing_nullable=True,
        )
        batch_op.drop_column("imap_password")
        batch_op.drop_column("imap_port")
        batch_op.drop_column("imap_host")
