import enum
import secrets
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


# Excludes visually-ambiguous characters (0/O, 1/I/L) -- this code is meant
# to be read aloud or retyped by a prospective teammate, so those pairs are
# a source of real support tickets, not a security concern.
_JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_join_code() -> str:
    return "".join(secrets.choice(_JOIN_CODE_ALPHABET) for _ in range(8))


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Comma-joined list of selected nonprofit types (the API accepts/returns
    # a real list[str] -- see OrganizationOut/OrganizationCreateRequest in
    # app/api/v1/schemas.py -- this column just stores the joined form,
    # since a nonprofit's type tags are never individually filtered/queried).
    nonprofit_type: Mapped[str | None] = mapped_column(String(500))
    # Shareable code a prospective teammate can enter to *request* to join
    # (see POST /v1/organizations/join) -- distinct from an owner-sent
    # invite: the Owner still has to approve the request and assign a role
    # before it grants any access (app/api/v1/join_requests.py).
    join_code: Mapped[str] = mapped_column(
        String(12), unique=True, index=True, nullable=False, default=generate_join_code
    )
    plan_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    # Record-keeping only -- when a platform admin's plan grant began. Doesn't
    # gate anything; the plan applies immediately on assignment (see
    # app/api/v1/admin.py::update_subscription). Not a deferred-activation
    # trigger, unlike plan_expires_at below.
    plan_starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When set, app/domain/subscriptions.py::revert_expired_plans (polled by
    # the background loop in app/main.py) reverts plan_id back to the Starter
    # plan once this passes, unless a platform admin renews/changes it first.
    plan_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

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
