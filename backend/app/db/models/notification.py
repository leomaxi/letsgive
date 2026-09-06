import enum
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin


class NotificationType(str, enum.Enum):
    APPROVAL_CODE = "approval_code"


class Notification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An in-app notification for one user. Introduced so a finance officer
    sees a session's approval code inside the app itself, not only in an
    email they may not have open (see request_approval in
    app/api/v1/sessions.py) -- the plaintext code lives here the same way it
    already does in the email an SmtpNotifier sends, not a new exposure.
    """

    __tablename__ = "notifications"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sessions.id"))
    type: Mapped[NotificationType] = mapped_column(
        SAEnum(
            NotificationType,
            name="notification_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
