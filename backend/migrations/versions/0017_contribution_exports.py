"""add contribution export templates

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("contribution_events", sa.Column("export_details", sa.JSON(), nullable=True))
    op.create_table(
        "contribution_export_templates",
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("field_keys", sa.JSON(), nullable=False),
        sa.Column("field_labels", sa.JSON(), nullable=False),
        sa.Column("sample", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_contribution_export_templates_organization_id"),
        "contribution_export_templates",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_contribution_export_templates_organization_id"),
        table_name="contribution_export_templates",
    )
    op.drop_table("contribution_export_templates")
    op.drop_column("contribution_events", "export_details")
