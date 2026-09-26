from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SessionMailboxConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "session_mailbox_connections"
    __table_args__ = (
        UniqueConstraint("session_id", "mailbox_connection_id", name="uq_session_mailbox_connection"),
    )

    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id"), nullable=False, index=True)
    mailbox_connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mailbox_connections.id"), nullable=False, index=True
    )
