"""add package limits, discounts, export usage, and session mailboxes

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLAN_UPDATES = {
    "starter": {
        "name": "Starter",
        "max_sessions_per_month": 2,
        "monthly_price_cents": 0,
        "max_exports_per_month": 0,
        "max_session_mailbox_connections": 1,
    },
    "growth": {
        "name": "Growth",
        "max_sessions_per_month": 3,
        "monthly_price_cents": 700,
        "max_exports_per_month": 3,
        "max_session_mailbox_connections": 1,
    },
    "premium": {
        "name": "Premium",
        "max_sessions_per_month": 8,
        "monthly_price_cents": 1300,
        "max_exports_per_month": 10,
        "max_session_mailbox_connections": 2,
    },
    "enterprise": {
        "name": "Enterprise / Diocese / Zone",
        "max_sessions_per_month": 1000,
        "monthly_price_cents": 4500,
        "max_exports_per_month": None,
        "max_session_mailbox_connections": None,
    },
}


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(sa.Column("max_exports_per_month", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("max_session_mailbox_connections", sa.Integer(), nullable=True))

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column("discount_percent", sa.Integer(), nullable=False, server_default="0")
        )

    plans = sa.table(
        "plans",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
        sa.column("max_sessions_per_month", sa.Integer),
        sa.column("monthly_price_cents", sa.Integer),
        sa.column("max_exports_per_month", sa.Integer),
        sa.column("max_session_mailbox_connections", sa.Integer),
    )
    bind = op.get_bind()
    for key, values in PLAN_UPDATES.items():
        bind.execute(plans.update().where(plans.c.key == key).values(**values))

    op.create_table(
        "session_mailbox_connections",
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("mailbox_connection_id", sa.String(length=36), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["mailbox_connection_id"], ["mailbox_connections.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "mailbox_connection_id", name="uq_session_mailbox_connection"),
    )
    op.create_index(
        op.f("ix_session_mailbox_connections_mailbox_connection_id"),
        "session_mailbox_connections",
        ["mailbox_connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_mailbox_connections_session_id"),
        "session_mailbox_connections",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "export_usage",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "month", name="uq_export_usage_org_month"),
    )
    op.create_index(op.f("ix_export_usage_organization_id"), "export_usage", ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_export_usage_organization_id"), table_name="export_usage")
    op.drop_table("export_usage")
    op.drop_index(op.f("ix_session_mailbox_connections_session_id"), table_name="session_mailbox_connections")
    op.drop_index(
        op.f("ix_session_mailbox_connections_mailbox_connection_id"),
        table_name="session_mailbox_connections",
    )
    op.drop_table("session_mailbox_connections")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("discount_percent")
    with op.batch_alter_table("plans") as batch_op:
        batch_op.drop_column("max_session_mailbox_connections")
        batch_op.drop_column("max_exports_per_month")
