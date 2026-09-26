"""add bonus sessions, promotions, and annual billing settings

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-26

"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE notification_type ADD VALUE IF NOT EXISTS 'billing_notice'")

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(sa.Column("bonus_sessions", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("bonus_sessions_expires_at", sa.DateTime(timezone=True), nullable=True))

    billing_interval = sa.Enum("monthly", "yearly", name="billing_interval")
    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.add_column(sa.Column("interval", billing_interval, nullable=False, server_default="monthly"))
        batch_op.add_column(sa.Column("annual_savings_percent", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("promotion_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("regular_amount", sa.Numeric(10, 2), nullable=True))
        batch_op.add_column(sa.Column("promo_cycles", sa.Integer(), nullable=False, server_default="0"))

    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(sa.Column("paypal_monthly_plan_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("paypal_yearly_plan_id", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_plans_paypal_monthly_plan_id", ["paypal_monthly_plan_id"])
        batch_op.create_unique_constraint("uq_plans_paypal_yearly_plan_id", ["paypal_yearly_plan_id"])

    op.create_table(
        "billing_promotions",
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=True),
        sa.Column("applies_to_existing", sa.Boolean(), nullable=False),
        sa.Column("applies_to_new", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_billing_promotions_plan_id"), "billing_promotions", ["plan_id"], unique=False)

    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.create_foreign_key(
            "fk_billing_subscriptions_promotion_id",
            "billing_promotions",
            ["promotion_id"],
            ["id"],
        )

    op.create_table(
        "billing_settings",
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column("annual_savings_percent", sa.Integer(), nullable=False),
        sa.Column("paypal_product_id", sa.String(length=64), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        sa.table(
            "billing_settings",
            sa.column("id", sa.String),
            sa.column("key", sa.String),
            sa.column("annual_savings_percent", sa.Integer),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        ),
        [
            {
                "id": str(uuid.uuid4()),
                "key": "default",
                "annual_savings_percent": 8,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("billing_settings")
    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.drop_constraint("fk_billing_subscriptions_promotion_id", type_="foreignkey")
    op.drop_index(op.f("ix_billing_promotions_plan_id"), table_name="billing_promotions")
    op.drop_table("billing_promotions")
    with op.batch_alter_table("plans") as batch_op:
        batch_op.drop_constraint("uq_plans_paypal_yearly_plan_id", type_="unique")
        batch_op.drop_constraint("uq_plans_paypal_monthly_plan_id", type_="unique")
        batch_op.drop_column("paypal_yearly_plan_id")
        batch_op.drop_column("paypal_monthly_plan_id")
    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.drop_column("promo_cycles")
        batch_op.drop_column("regular_amount")
        batch_op.drop_column("promotion_id")
        batch_op.drop_column("annual_savings_percent")
        batch_op.drop_column("interval")
    sa.Enum(name="billing_interval").drop(op.get_bind(), checkfirst=True)
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("bonus_sessions_expires_at")
        batch_op.drop_column("bonus_sessions")
