from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.session import Session

MAX_APPROVAL_ATTEMPTS = 5
APPROVAL_TTL_MINUTES = 10


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A finance one-time code issued to authorize a session's monitoring window.

    code_hash stores an Argon2 hash, never the plaintext code. Once verified or
    locked out, a row is never reused for authorization -- request-approval always
    creates a new row so history stays intact for audit (spec 8: 'Auditability').
    """

    __tablename__ = "approvals"

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    approver_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))

    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    session: Mapped["Session"] = relationship(back_populates="approvals")
