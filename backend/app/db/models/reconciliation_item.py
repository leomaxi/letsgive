import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, ForeignKey, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class ReconciliationStatus(str, enum.Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


class ReconciliationResolution(str, enum.Enum):
    ACCEPTED = "accepted"
    EXCLUDED = "excluded"
    # This item's underlying event is itself a notification that an earlier
    # ACCEPTED contribution was returned/reversed -- see
    # ReconciliationResolveRequest.reverses_event_id and app/domain/ledger.py.
    REVERSED = "reversed"
    # Same ledger effect as EXCLUDED (nothing new created, the original
    # event's own decision is left untouched) -- tracked as its own value
    # purely so a reviewer's reasoning survives in reports/audit instead of
    # collapsing into a generic "excluded".
    DUPLICATE = "duplicate"


class ReconciliationItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A finance review queue entry for one ambiguous/borderline ledger event
    (spec 7 / 10 'reconciliation_items'). Only created for events that are
    genuinely worth a human look -- AMBIGUOUS (matched a parser profile but
    below its confidence threshold) and EXCLUDED_TIME_WINDOW (arrived just
    outside the session's approved window, possibly a legitimate late
    notification). Confidently-rejected events (wrong sender, no credit
    intent) aren't queued; they were never ambiguous.

    Resolving as ACCEPTED or REVERSED never mutates an existing
    ContributionEvent -- both create a new one (see
    ContributionEvent.corrects_event_id) so the original evidence is
    preserved exactly as ingested (spec 7: "Corrections append events and
    preserve original evidence"). EXCLUDED and DUPLICATE create nothing;
    they just record why the item was closed without becoming a
    contribution.
    """

    __tablename__ = "reconciliation_items"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), nullable=False, index=True
    )
    contribution_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("contribution_events.id"), nullable=False
    )

    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[ReconciliationStatus] = mapped_column(
        SAEnum(
            ReconciliationStatus,
            name="reconciliation_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ReconciliationStatus.PENDING,
    )

    resolution: Mapped[ReconciliationResolution | None] = mapped_column(
        SAEnum(
            ReconciliationResolution,
            name="reconciliation_resolution",
            values_callable=lambda e: [m.value for m in e],
        ),
    )
    resolver_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution_note: Mapped[str | None] = mapped_column(String(500))
    corrected_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))

    # What a reviewer can actually see, given spec 8's data-minimization rule
    # against retaining raw email content -- there's no message body to point
    # to, so "evidence" is the ledger event's own normalized fields.
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
