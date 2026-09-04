"""add per-plan quotas for display templates and team-member seats

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# key -> (max_display_templates, max_team_members), matching app/db/models/plan.py's SEED_PLANS.
QUOTAS_BY_PLAN_KEY = {
    "starter": (2, 5),
    "growth": (5, 15),
    "premium": (20, 50),
    "enterprise": (100, 500),
}


def upgrade() -> None:
    with op.batch_alter_table("plans") as batch_op:
        batch_op.add_column(
            sa.Column("max_display_templates", sa.Integer(), nullable=False, server_default="2")
        )
        batch_op.add_column(
            sa.Column("max_team_members", sa.Integer(), nullable=False, server_default="5")
        )

    plans = sa.table(
        "plans",
        sa.column("key", sa.String),
        sa.column("max_display_templates", sa.Integer),
        sa.column("max_team_members", sa.Integer),
    )
    bind = op.get_bind()
    for key, (max_templates, max_members) in QUOTAS_BY_PLAN_KEY.items():
        bind.execute(
            plans.update()
            .where(plans.c.key == key)
            .values(max_display_templates=max_templates, max_team_members=max_members)
        )


def downgrade() -> None:
    with op.batch_alter_table("plans") as batch_op:
        batch_op.drop_column("max_team_members")
        batch_op.drop_column("max_display_templates")
