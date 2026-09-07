from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SupportTicketMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "support_ticket_messages"

    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("support_tickets.id"), nullable=False, index=True
    )
    author_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    # Persisted at write time from whichever dependency authenticated the
    # author (get_platform_admin vs get_membership), not derived from
    # User.is_platform_admin at read time -- a later staff access change
    # can't retroactively rewrite which side of the conversation a past
    # message came from.
    author_is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False)
    body: Mapped[str] = mapped_column(String(4000), nullable=False)
