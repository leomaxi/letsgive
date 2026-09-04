from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

# Illustrative tiers from spec 13. Real pricing/invoicing needs a payment
# provider (Stripe et al.) this environment has no credentials for -- see
# README "known gaps". What's real here is entitlement enforcement: these
# limits are actually checked server-side (spec 13: "Billing entitlements
# must be enforced server-side"), not just displayed.
SEED_PLANS = [
    {
        "key": "starter",
        "name": "Starter",
        "max_sessions_per_month": 4,
        "max_mailbox_connections": 1,
        "max_display_templates": 2,
        "max_team_members": 5,
        "allows_custom_subdomain": False,
        "allows_sso": False,
    },
    {
        "key": "growth",
        "name": "Growth",
        "max_sessions_per_month": 20,
        "max_mailbox_connections": 3,
        "max_display_templates": 5,
        "max_team_members": 15,
        "allows_custom_subdomain": False,
        "allows_sso": False,
    },
    {
        "key": "premium",
        "name": "Premium",
        "max_sessions_per_month": 100,
        "max_mailbox_connections": 10,
        "max_display_templates": 20,
        "max_team_members": 50,
        "allows_custom_subdomain": True,
        "allows_sso": True,
    },
    {
        "key": "enterprise",
        "name": "Enterprise / Diocese",
        "max_sessions_per_month": 1000,
        "max_mailbox_connections": 100,
        "max_display_templates": 100,
        "max_team_members": 500,
        "allows_custom_subdomain": True,
        "allows_sso": True,
    },
]


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    max_sessions_per_month: Mapped[int] = mapped_column(Integer, nullable=False)
    max_mailbox_connections: Mapped[int] = mapped_column(Integer, nullable=False)
    max_display_templates: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    max_team_members: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    allows_custom_subdomain: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allows_sso: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
