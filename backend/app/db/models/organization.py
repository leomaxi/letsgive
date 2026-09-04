import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.membership import Membership


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    nonprofit_type: Mapped[str | None] = mapped_column(String(100))
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        SAEnum(
            SubscriptionStatus,
            name="subscription_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SubscriptionStatus.TRIALING,
    )
    # Read-only access to historical reports continues until this instant
    # after cancellation (spec 13); new sessions/connections/templates stop
    # immediately on cancel, not at grace-period end.
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization")
