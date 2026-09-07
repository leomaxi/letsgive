"""add platform admin flag and support ticket tables

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every existing user defaults to False -- no backfill needed.
    op.add_column(
        "users",
        sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "in_progress", "resolved", "closed", name="support_ticket_status"),
            nullable=False,
            server_default="open",
        ),
        sa.Column("admin_unread", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_support_tickets_organization_id", "support_tickets", ["organization_id"])

    op.create_table(
        "support_ticket_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), sa.ForeignKey("support_tickets.id"), nullable=False),
        sa.Column("author_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("author_is_admin", sa.Boolean(), nullable=False),
        sa.Column("body", sa.String(length=4000), nullable=False),
    )
    op.create_index(
        "ix_support_ticket_messages_ticket_id", "support_ticket_messages", ["ticket_id"]
    )

    # Widen notification_type with 'support_reply' -- same dialect-specific
    # shape as migration 0008: SQLite stores this as plain TEXT (no native
    # enum type, nothing to alter); MySQL has no separate named enum type, so
    # widening it is a single MODIFY COLUMN restating the full new value
    # list; Postgres needs its own ALTER TYPE ... ADD VALUE statement.
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.alter_column(
            "notifications",
            "type",
            existing_type=sa.Enum("approval_code", name="notification_type"),
            type_=sa.Enum("approval_code", "support_reply", name="notification_type"),
            existing_nullable=False,
        )
    elif bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'support_reply'")


def downgrade() -> None:
    op.drop_index("ix_support_ticket_messages_ticket_id", table_name="support_ticket_messages")
    op.drop_table("support_ticket_messages")
    op.drop_index("ix_support_tickets_organization_id", table_name="support_tickets")
    op.drop_table("support_tickets")
    op.drop_column("users", "is_platform_admin")
    # notification_type enum widening is not reversed -- same rationale as
    # migration 0008 (no supported way to safely drop a Postgres enum label
    # short of recreating the type, and not worth the risk on MySQL either).
