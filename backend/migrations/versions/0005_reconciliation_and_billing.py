"""reconciliation items, corrections, plans, subscription status

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_PLANS = [
    {
        "key": "starter",
        "name": "Starter",
        "max_sessions_per_month": 4,
        "max_mailbox_connections": 1,
        "allows_custom_subdomain": False,
        "allows_sso": False,
    },
    {
        "key": "growth",
        "name": "Growth",
        "max_sessions_per_month": 20,
        "max_mailbox_connections": 3,
        "allows_custom_subdomain": False,
        "allows_sso": False,
    },
    {
        "key": "premium",
        "name": "Premium",
        "max_sessions_per_month": 100,
        "max_mailbox_connections": 10,
        "allows_custom_subdomain": True,
        "allows_sso": True,
    },
    {
        "key": "enterprise",
        "name": "Enterprise / Diocese",
        "max_sessions_per_month": 1000,
        "max_mailbox_connections": 100,
        "allows_custom_subdomain": True,
        "allows_sso": True,
    },
]


def upgrade() -> None:
    plans_table = op.create_table(
        "plans",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("key", sa.String(length=50), nullable=False, unique=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("max_sessions_per_month", sa.Integer(), nullable=False),
        sa.Column("max_mailbox_connections", sa.Integer(), nullable=False),
        sa.Column("allows_custom_subdomain", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allows_sso", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    seeded_at = datetime.now(timezone.utc)
    op.bulk_insert(
        plans_table,
        [
            {"id": str(uuid.uuid4()), "created_at": seeded_at, "updated_at": seeded_at, **plan}
            for plan in SEED_PLANS
        ],
    )

    subscription_status = sa.Enum(
        "trialing", "active", "past_due", "canceled", name="subscription_status"
    )
    subscription_status.create(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "subscription_status",
                subscription_status,
                nullable=False,
                server_default="trialing",
            )
        )
        batch_op.add_column(sa.Column("grace_period_ends_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_organizations_plan_id", "plans", ["plan_id"], ["id"]
        )

    with op.batch_alter_table("contribution_events") as batch_op:
        batch_op.add_column(
            sa.Column(
                "corrects_event_id",
                sa.String(length=36),
                sa.ForeignKey(
                    "contribution_events.id", name="fk_contribution_events_corrects_event_id"
                ),
                nullable=True,
            )
        )

    reconciliation_status = sa.Enum("pending", "resolved", name="reconciliation_status")
    reconciliation_resolution = sa.Enum("accepted", "excluded", name="reconciliation_resolution")

    op.create_table(
        "reconciliation_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column(
            "contribution_event_id",
            sa.String(length=36),
            sa.ForeignKey("contribution_events.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("status", reconciliation_status, nullable=False, server_default="pending"),
        sa.Column("resolution", reconciliation_resolution, nullable=True),
        sa.Column("resolver_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(length=500), nullable=True),
        sa.Column("corrected_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_reconciliation_items_organization_id", "reconciliation_items", ["organization_id"]
    )
    op.create_index("ix_reconciliation_items_session_id", "reconciliation_items", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_reconciliation_items_session_id", table_name="reconciliation_items")
    op.drop_index("ix_reconciliation_items_organization_id", table_name="reconciliation_items")
    op.drop_table("reconciliation_items")
    sa.Enum(name="reconciliation_resolution").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="reconciliation_status").drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("contribution_events") as batch_op:
        batch_op.drop_constraint(
            "fk_contribution_events_corrects_event_id", type_="foreignkey"
        )
        batch_op.drop_column("corrects_event_id")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_constraint("fk_organizations_plan_id", type_="foreignkey")
        batch_op.drop_column("grace_period_ends_at")
        batch_op.drop_column("subscription_status")

    sa.Enum(name="subscription_status").drop(op.get_bind(), checkfirst=True)
    op.drop_table("plans")
