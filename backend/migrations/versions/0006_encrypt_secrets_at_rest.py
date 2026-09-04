"""widen mfa_secret and webhook_secret for envelope encryption at rest

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Both columns now store a Fernet token (app/domain/crypto.py) instead of
# the raw plaintext value -- meaningfully longer than the plaintext ever
# was, so the column needs to be widened to fit it. No data backfill here:
# this project has no production deployment yet, so there's no existing
# plaintext data in either column to re-encrypt in place.
def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "mfa_secret",
            existing_type=sa.String(length=64),
            type_=sa.String(length=500),
            existing_nullable=True,
        )

    with op.batch_alter_table("mailbox_connections") as batch_op:
        batch_op.alter_column(
            "webhook_secret",
            existing_type=sa.String(length=64),
            type_=sa.String(length=500),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("mailbox_connections") as batch_op:
        batch_op.alter_column(
            "webhook_secret",
            existing_type=sa.String(length=500),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "mfa_secret",
            existing_type=sa.String(length=500),
            type_=sa.String(length=64),
            existing_nullable=True,
        )
