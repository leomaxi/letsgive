import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.approval import Approval


class SessionStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVAL_REQUESTED = "approval_requested"
    AUTHORIZED = "authorized"
    LIVE = "live"
    PAUSED = "paused"
    ENDED = "ended"
    RECONCILING = "reconciling"
    CLOSED = "closed"


# Transitions Phase 2 implements. reconciling/closed are reachable only once
# Phase 5 reconciliation exists, but the states are modeled now for schema stability.
ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.DRAFT: {SessionStatus.APPROVAL_REQUESTED},
    SessionStatus.APPROVAL_REQUESTED: {SessionStatus.APPROVAL_REQUESTED, SessionStatus.AUTHORIZED},
    SessionStatus.AUTHORIZED: {SessionStatus.LIVE, SessionStatus.ENDED},
    SessionStatus.LIVE: {SessionStatus.PAUSED, SessionStatus.ENDED},
    SessionStatus.PAUSED: {SessionStatus.LIVE, SessionStatus.ENDED},
    SessionStatus.ENDED: set(),
    SessionStatus.RECONCILING: {SessionStatus.CLOSED},
    SessionStatus.CLOSED: set(),
}


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sessions"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    mailbox_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mailbox_connections.id")
    )

    status: Mapped[SessionStatus] = mapped_column(
        SAEnum(SessionStatus, name="session_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=SessionStatus.DRAFT,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    display_template_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("display_templates.id")
    )
    contribution_method: Mapped[str] = mapped_column(String(50), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    watermark: Mapped[datetime | None] = mapped_column(UTCDateTime)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    goal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    goal_amount: Mapped[float | None] = mapped_column(Numeric(12, 2))

    amount_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    operator_warning: Mapped[str | None] = mapped_column(String(300))

    approvals: Mapped[list["Approval"]] = relationship(back_populates="session")
