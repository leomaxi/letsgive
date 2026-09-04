"""mailbox connections, parser profiles, contribution ledger

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    mailbox_provider = sa.Enum("fake", "microsoft", "gmail", name="mailbox_provider")
    connection_status = sa.Enum("pending", "connected", "error", "revoked", name="connection_status")

    op.create_table(
        "mailbox_connections",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("provider", mailbox_provider, nullable=False),
        sa.Column("mailbox", sa.String(length=255), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=True),
        sa.Column("status", connection_status, nullable=False, server_default="pending"),
        sa.Column("token_ref", sa.String(length=255), nullable=True),
        sa.Column("external_account_id", sa.String(length=255), nullable=True),
        sa.Column("webhook_secret", sa.String(length=64), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_health", sa.String(length=50), nullable=True),
        sa.Column("is_dedicated_mailbox", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mailbox_connections_organization_id", "mailbox_connections", ["organization_id"])

    op.create_table(
        "parser_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("template_version", sa.String(length=50), nullable=False, server_default="v1"),
        sa.Column("sender_patterns", sa.JSON(), nullable=False),
        sa.Column("credit_keywords", sa.JSON(), nullable=False),
        sa.Column("reject_keywords", sa.JSON(), nullable=False),
        sa.Column("amount_pattern", sa.String(length=500), nullable=False),
        sa.Column("default_currency", sa.String(length=3), nullable=False, server_default="CAD"),
        sa.Column("confidence_threshold", sa.Float(), nullable=False, server_default="0.75"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_parser_profiles_organization_id", "parser_profiles", ["organization_id"])

    # SQLite can't ALTER TABLE ADD COLUMN with an inline FK constraint outside
    # of batch mode (it rebuilds the table under the hood); Postgres would
    # have accepted a plain add_column, but batch mode works on both.
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "mailbox_connection_id",
                sa.String(length=36),
                sa.ForeignKey(
                    "mailbox_connections.id", name="fk_sessions_mailbox_connection_id"
                ),
                nullable=True,
            )
        )

    contribution_decision = sa.Enum(
        "accepted",
        "ambiguous",
        "excluded_time_window",
        "excluded_source_mismatch",
        "excluded_no_credit_intent",
        "excluded_no_active_session",
        name="contribution_decision",
    )

    op.create_table(
        "contribution_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False
        ),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("sessions.id"), nullable=True),
        sa.Column(
            "mailbox_connection_id",
            sa.String(length=36),
            sa.ForeignKey("mailbox_connections.id"),
            nullable=True,
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", contribution_decision, nullable=False),
        sa.Column("decision_reason", sa.String(length=300), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("parser_confidence", sa.Float(), nullable=True),
        sa.Column("template_version", sa.String(length=50), nullable=True),
        sa.Column("test_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "organization_id",
            "mailbox_connection_id",
            "provider_message_id",
            name="uq_contribution_event_provider_message",
        ),
    )
    op.create_index("ix_contribution_events_organization_id", "contribution_events", ["organization_id"])
    op.create_index("ix_contribution_events_session_id", "contribution_events", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_contribution_events_session_id", table_name="contribution_events")
    op.drop_index("ix_contribution_events_organization_id", table_name="contribution_events")
    op.drop_table("contribution_events")
    sa.Enum(name="contribution_decision").drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_column("mailbox_connection_id")

    op.drop_index("ix_parser_profiles_organization_id", table_name="parser_profiles")
    op.drop_table("parser_profiles")

    op.drop_index("ix_mailbox_connections_organization_id", table_name="mailbox_connections")
    op.drop_table("mailbox_connections")
    sa.Enum(name="connection_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="mailbox_provider").drop(op.get_bind(), checkfirst=True)
