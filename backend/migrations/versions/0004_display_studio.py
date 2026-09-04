"""display templates, elements, and session template binding

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "display_templates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("canvas", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_display_templates_organization_id", "display_templates", ["organization_id"])

    display_element_type = sa.Enum(
        "logo",
        "heading",
        "body_text",
        "contribution_count",
        "amount",
        "goal",
        "progress_bar",
        "countdown",
        "payment_instructions",
        "qr_code",
        "background",
        "sponsor_message",
        name="display_element_type",
    )

    op.create_table(
        "display_elements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "template_id",
            sa.String(length=36),
            sa.ForeignKey("display_templates.id"),
            nullable=False,
        ),
        sa.Column("type", display_element_type, nullable=False),
        sa.Column("x", sa.Float(), nullable=False, server_default="0"),
        sa.Column("y", sa.Float(), nullable=False, server_default="0"),
        sa.Column("width", sa.Float(), nullable=False, server_default="200"),
        sa.Column("height", sa.Float(), nullable=False, server_default="100"),
        sa.Column("z_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("style", sa.JSON(), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_display_elements_template_id", "display_elements", ["template_id"])

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("display_template_ref")
        batch_op.add_column(
            sa.Column(
                "display_template_id",
                sa.String(length=36),
                sa.ForeignKey("display_templates.id", name="fk_sessions_display_template_id"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("display_template_id")
        batch_op.add_column(sa.Column("display_template_ref", sa.String(length=100), nullable=True))

    op.drop_index("ix_display_elements_template_id", table_name="display_elements")
    op.drop_table("display_elements")
    sa.Enum(name="display_element_type").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_display_templates_organization_id", table_name="display_templates")
    op.drop_table("display_templates")
