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
        "max_sessions_per_month": 2,
        "max_mailbox_connections": 1,
        "max_display_templates": 2,
        "max_team_members": 5,
        "allows_custom_subdomain": False,
        "allows_sso": False,
        "monthly_price_cents": 0,
        "max_exports_per_month": 0,
        "max_session_mailbox_connections": 1,
    },
    {
        "key": "growth",
        "name": "Growth",
        "max_sessions_per_month": 3,
        "max_mailbox_connections": 3,
        "max_display_templates": 5,
        "max_team_members": 15,
        "allows_custom_subdomain": False,
        "allows_sso": False,
        "monthly_price_cents": 700,
        "max_exports_per_month": 3,
        "max_session_mailbox_connections": 1,
    },
    {
        "key": "premium",
        "name": "Premium",
        "max_sessions_per_month": 8,
        "max_mailbox_connections": 10,
        "max_display_templates": 20,
        "max_team_members": 50,
        "allows_custom_subdomain": True,
        "allows_sso": True,
        "monthly_price_cents": 1300,
        "max_exports_per_month": 10,
        "max_session_mailbox_connections": 2,
    },
    {
        "key": "enterprise",
        "name": "Enterprise / Diocese / Zone",
        "max_sessions_per_month": 1000,
        "max_mailbox_connections": 100,
        "max_display_templates": 100,
        "max_team_members": 500,
        "allows_custom_subdomain": True,
        "allows_sso": True,
        "monthly_price_cents": 4500,
        "max_exports_per_month": None,
        "max_session_mailbox_connections": None,
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
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paypal_plan_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Auto-provisioned PayPal billing plans (intro cycle + regular cycle, see
    # app/domain/subscriptions.py::ensure_paypal_plan_id). Created on first
    # checkout for each interval so admins never have to map plans by hand.
    paypal_monthly_plan_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    paypal_yearly_plan_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    max_exports_per_month: Mapped[int | None] = mapped_column(Integer)
    max_session_mailbox_connections: Mapped[int | None] = mapped_column(Integer, default=1)
