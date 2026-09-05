import enum
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class ContributionDecision(str, enum.Enum):
    ACCEPTED = "accepted"
    AMBIGUOUS = "ambiguous"
    EXCLUDED_TIME_WINDOW = "excluded_time_window"
    EXCLUDED_SOURCE_MISMATCH = "excluded_source_mismatch"
    EXCLUDED_NO_CREDIT_INTENT = "excluded_no_credit_intent"
    EXCLUDED_NO_ACTIVE_SESSION = "excluded_no_active_session"
    # A reconciliation-generated correction only (see ReconciliationResolution
    # .REVERSED / app/domain/ledger.py) -- never a first-pass parser decision.
    # Nets *out* of a session's count/total rather than adding to them.
    REVERSED = "reversed"


class ContributionEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A normalized, immutable record derived from one deposit-notification
    message (spec 9.2 'ledger'). ACCEPTED events count toward a session's
    public/operator totals; REVERSED events (reconciliation-generated only)
    net back out of them. Those totals are always derived by aggregating
    this table (see app/domain/ledger.py), never a separately-mutated
    counter (spec 10.1), so there is nothing to race or double-increment.

    Deliberately excludes sender identity, subject and body: spec 8 requires
    minimizing retained email content, and the public/operator APIs must
    never be able to leak donor identity even by a future bug -- the field
    simply doesn't exist here.
    """

    __tablename__ = "contribution_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "mailbox_connection_id",
            "provider_message_id",
            name="uq_contribution_event_provider_message",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id"), index=True
    )
    # Nullable: synthetic test-mode deposits (simulate-deposit) aren't tied to
    # a real mailbox at all. Real ingested events always set this.
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mailbox_connections.id")
    )

    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    # Set only on a reconciliation-generated correction row: points at the
    # original (still-untouched) event it supersedes for counting purposes
    # (an ACCEPTED correction with a corrected amount) or reverses (a
    # REVERSED correction, pointing back at the ACCEPTED event it un-does).
    corrects_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("contribution_events.id")
    )

    decision: Mapped[ContributionDecision] = mapped_column(
        SAEnum(
            ContributionDecision,
            name="contribution_decision",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    decision_reason: Mapped[str | None] = mapped_column(String(300))

    amount: Mapped[float | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    parser_confidence: Mapped[float | None] = mapped_column(Float)
    template_version: Mapped[str | None] = mapped_column(String(50))

    test_mode: Mapped[bool] = mapped_column(nullable=False, default=False)
