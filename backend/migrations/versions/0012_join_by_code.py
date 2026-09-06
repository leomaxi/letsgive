"""add organizations.join_code, a REQUESTED membership status, and make
membership.role nullable (a join request has no role until the Owner
approves it and assigns one)

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-07

"""
import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_join_code() -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(8))


def upgrade() -> None:
    op.add_column("organizations", sa.Column("join_code", sa.String(length=12), nullable=True))

    bind = op.get_bind()
    org_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM organizations")).fetchall()]
    used: set[str] = set()
    for org_id in org_ids:
        code = _generate_join_code()
        while code in used:
            code = _generate_join_code()
        used.add(code)
        bind.execute(
            sa.text("UPDATE organizations SET join_code = :code WHERE id = :id"),
            {"code": code, "id": org_id},
        )

    with op.batch_alter_table("organizations") as batch_op:
        batch_op.alter_column("join_code", existing_type=sa.String(length=12), nullable=False)
    op.create_index("ix_organizations_join_code", "organizations", ["join_code"], unique=True)

    with op.batch_alter_table("memberships") as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.Enum("owner", "finance", "media", "auditor", "system_admin", name="membership_role"),
            nullable=True,
        )

    if bind.dialect.name == "mysql":
        op.alter_column(
            "memberships",
            "status",
            existing_type=sa.Enum("invited", "active", "suspended", name="membership_status"),
            type_=sa.Enum("invited", "active", "suspended", "requested", name="membership_status"),
            existing_nullable=False,
        )
    elif bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE membership_status ADD VALUE IF NOT EXISTS 'requested'")


def downgrade() -> None:
    # See migration 0008 for why a Postgres enum value isn't removed on
    # downgrade (no supported way short of recreating the type).
    with op.batch_alter_table("memberships") as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.Enum("owner", "finance", "media", "auditor", "system_admin", name="membership_role"),
            nullable=False,
        )
    op.drop_index("ix_organizations_join_code", table_name="organizations")
    with op.batch_alter_table("organizations") as batch_op:
        batch_op.drop_column("join_code")
