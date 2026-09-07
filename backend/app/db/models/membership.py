import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.db.models.organization import Organization
    from app.db.models.user import User


class Role(str, enum.Enum):
    OWNER = "owner"
    FINANCE = "finance"
    MEDIA = "media"
    AUDITOR = "auditor"
    # Unused/unwired since the initial migration -- an org-scoped Membership
    # role is the wrong shape for real platform-admin access (which needs to
    # be global, not per-org). See User.is_platform_admin + get_platform_admin
    # in app/api/v1/deps.py for the actual platform-admin mechanism. Left in
    # place rather than removed: dropping an enum label needs its own
    # dialect-specific migration branch (see migration 0008) for zero
    # functional benefit, since nothing ever assigns this value.
    SYSTEM_ADMIN = "system_admin"


# Roles that must have MFA enabled before they can be granted, per spec 3.1.
MFA_REQUIRED_ROLES = {Role.OWNER, Role.FINANCE}


class MembershipStatus(str, enum.Enum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    # Self-service join-by-code (POST /v1/organizations/join): the requester
    # picked an org by code, not a role -- the Owner assigns one on approval
    # (see app/api/v1/join_requests.py). Never granted any access itself:
    # get_membership/load_active_membership only ever return ACTIVE rows.
    REQUESTED = "requested"


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_membership_user_org"),)

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    # Nullable only for a REQUESTED row, which by definition has no role
    # assigned yet -- every other status always has one (enforced in the API
    # layer: a request is only ever created without a role, and can only
    # transition to ACTIVE by the approve endpoint, which requires one).
    role: Mapped[Role | None] = mapped_column(
        SAEnum(
            Role,
            name="membership_role",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
    )
    status: Mapped[MembershipStatus] = mapped_column(
        SAEnum(
            MembershipStatus,
            name="membership_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=MembershipStatus.ACTIVE,
    )
    access_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")
