import enum

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SupportTicketStatus(str, enum.Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SupportTicket(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant's support conversation with platform-admin staff (see
    app/api/v1/support.py for the tenant side, app/api/v1/admin.py for the
    admin side). Messages live in SupportTicketMessage.
    """

    __tablename__ = "support_tickets"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[SupportTicketStatus] = mapped_column(
        SAEnum(
            SupportTicketStatus,
            name="support_ticket_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SupportTicketStatus.OPEN,
    )
    # Flipped True whenever the tenant side posts a message, cleared when a
    # platform admin views or replies -- this IS the admin portal's "needs a
    # reply" inbox signal. No separate notification table for admin-side
    # awareness (a handful of staff accounts don't need one; see
    # NotificationsBell/Notification for the tenant-side equivalent instead).
    admin_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
