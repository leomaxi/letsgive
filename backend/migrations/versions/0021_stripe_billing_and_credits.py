"""switch billing to Stripe; purchasable session/export credits

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-07

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.add_column(sa.Column("session_credits", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("export_credits", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("stripe_customer_id", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_organizations_stripe_customer_id", ["stripe_customer_id"])

    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(sa.Column("stripe_product_id", sa.String(length=64), nullable=True))

    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    with op.batch_alter_table("billing_payments") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=30), nullable=False, server_default="subscription"))
        batch_op.add_column(sa.Column("description", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("receipt_url", sa.String(length=1000), nullable=True))

    with op.batch_alter_table("billing_settings") as batch_op:
        batch_op.add_column(sa.Column("stripe_secret_key", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("stripe_webhook_secret", sa.String(length=512), nullable=True))
        batch_op.add_column(
            sa.Column("session_topup_price_cents", sa.Integer(), nullable=False, server_default="300")
        )
        batch_op.add_column(
            sa.Column("export_topup_price_cents", sa.Integer(), nullable=False, server_default="100")
        )

    op.create_table(
        "credit_purchases",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("purchased_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_checkout_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["purchased_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_checkout_id"),
    )
    op.create_index("ix_credit_purchases_organization_id", "credit_purchases", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_credit_purchases_organization_id", table_name="credit_purchases")
    op.drop_table("credit_purchases")
    with op.batch_alter_table("billing_settings") as batch_op:
        batch_op.drop_column("export_topup_price_cents")
        batch_op.drop_column("session_topup_price_cents")
        batch_op.drop_column("stripe_webhook_secret")
        batch_op.drop_column("stripe_secret_key")
    with op.batch_alter_table("billing_payments") as batch_op:
        batch_op.drop_column("receipt_url")
        batch_op.drop_column("description")
        batch_op.drop_column("kind")
    with op.batch_alter_table("billing_subscriptions") as batch_op:
        batch_op.drop_column("cancel_at_period_end")
    with op.batch_alter_table("plans") as batch_op:
        batch_op.drop_column("stripe_product_id")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_index("ix_organizations_stripe_customer_id")
        batch_op.drop_column("stripe_customer_id")
        batch_op.drop_column("export_credits")
        batch_op.drop_column("session_credits")
