"""add PayPal subscription billing records

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PLAN_PRICES = {
    "starter": 0,
    "growth": 2900,
    "premium": 7900,
    "enterprise": 19900,
}


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(
            sa.Column("monthly_price_cents", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("paypal_plan_id", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint("uq_plans_paypal_plan_id", ["paypal_plan_id"])

    plans = sa.table(
        "plans",
        sa.column("key", sa.String),
        sa.column("monthly_price_cents", sa.Integer),
    )
    bind = op.get_bind()
    for key, price in PLAN_PRICES.items():
        bind.execute(plans.update().where(plans.c.key == key).values(monthly_price_cents=price))

    billing_subscription_status = sa.Enum(
        "approval_pending",
        "active",
        "past_due",
        "canceled",
        "suspended",
        name="billing_subscription_status",
    )
    billing_payment_status = sa.Enum(
        "completed", "refunded", "partially_refunded", name="billing_payment_status"
    )
    billing_refund_status = sa.Enum("pending", "completed", "failed", name="billing_refund_status")

    op.create_table(
        "billing_subscriptions",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_subscription_id", sa.String(length=128), nullable=False),
        sa.Column("status", billing_subscription_status, nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_subscription_id"),
    )
    op.create_index(
        op.f("ix_billing_subscriptions_organization_id"),
        "billing_subscriptions",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "billing_payments",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("billing_subscription_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=False),
        sa.Column("provider_capture_id", sa.String(length=128), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", billing_payment_status, nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["billing_subscription_id"], ["billing_subscriptions.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_id"),
    )
    op.create_index(
        op.f("ix_billing_payments_billing_subscription_id"),
        "billing_payments",
        ["billing_subscription_id"],
        unique=False,
    )
    op.create_index(
        "ix_billing_payments_org_created",
        "billing_payments",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_billing_payments_organization_id"),
        "billing_payments",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_billing_payments_provider_capture_id"),
        "billing_payments",
        ["provider_capture_id"],
        unique=False,
    )

    op.create_table(
        "billing_refunds",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("billing_payment_id", sa.String(length=36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_refund_id", sa.String(length=128), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", billing_refund_status, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["billing_payment_id"], ["billing_payments.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_refund_id"),
    )
    op.create_index(
        op.f("ix_billing_refunds_billing_payment_id"),
        "billing_refunds",
        ["billing_payment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_billing_refunds_organization_id"),
        "billing_refunds",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_billing_refunds_organization_id"), table_name="billing_refunds")
    op.drop_index(op.f("ix_billing_refunds_billing_payment_id"), table_name="billing_refunds")
    op.drop_table("billing_refunds")
    op.drop_index(op.f("ix_billing_payments_provider_capture_id"), table_name="billing_payments")
    op.drop_index(op.f("ix_billing_payments_organization_id"), table_name="billing_payments")
    op.drop_index("ix_billing_payments_org_created", table_name="billing_payments")
    op.drop_index(
        op.f("ix_billing_payments_billing_subscription_id"), table_name="billing_payments"
    )
    op.drop_table("billing_payments")
    op.drop_index(
        op.f("ix_billing_subscriptions_organization_id"), table_name="billing_subscriptions"
    )
    op.drop_table("billing_subscriptions")
    sa.Enum(name="billing_refund_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="billing_payment_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="billing_subscription_status").drop(op.get_bind(), checkfirst=True)
    with op.batch_alter_table("plans") as batch_op:
        batch_op.drop_constraint("uq_plans_paypal_plan_id", type_="unique")
        batch_op.drop_column("paypal_plan_id")
        batch_op.drop_column("monthly_price_cents")
