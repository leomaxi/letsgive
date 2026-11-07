import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.domain.crypto import EncryptedString


class BillingSubscriptionStatus(str, enum.Enum):
    APPROVAL_PENDING = "approval_pending"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    SUSPENDED = "suspended"


class BillingPaymentStatus(str, enum.Enum):
    COMPLETED = "completed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class BillingRefundStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class BillingInterval(str, enum.Enum):
    MONTHLY = "monthly"
    YEARLY = "yearly"


class BillingSubscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_subscriptions"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(36), ForeignKey("plans.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="paypal")
    provider_subscription_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    status: Mapped[BillingSubscriptionStatus] = mapped_column(
        SAEnum(
            BillingSubscriptionStatus,
            name="billing_subscription_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=BillingSubscriptionStatus.APPROVAL_PENDING,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Price charged once any promotional cycles are used up. Equal to amount
    # when no promotion applied.
    regular_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    promo_cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interval: Mapped[BillingInterval] = mapped_column(
        SAEnum(BillingInterval, name="billing_interval", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=BillingInterval.MONTHLY,
    )
    annual_savings_percent: Mapped[int | None] = mapped_column(Integer)
    promotion_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("billing_promotions.id"))
    current_period_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    canceled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Stripe: the owner stopped auto-renew; the plan stays until
    # current_period_end, then the org drops to the free Starter plan.
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class BillingPayment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_payments"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    billing_subscription_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("billing_subscriptions.id"), index=True
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="paypal")
    provider_payment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_capture_id: Mapped[str | None] = mapped_column(String(128), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[BillingPaymentStatus] = mapped_column(
        SAEnum(
            BillingPaymentStatus,
            name="billing_payment_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=BillingPaymentStatus.COMPLETED,
    )
    period_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # What the payment was for: "subscription", "session_credits",
    # "export_credits". Receipt link is Stripe's hosted invoice/receipt page.
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="subscription")
    description: Mapped[str | None] = mapped_column(String(255))
    receipt_url: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (UniqueConstraint("provider", "provider_payment_id", name="uq_payment_provider_id"),)


class BillingRefund(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_refunds"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    billing_payment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("billing_payments.id"), nullable=False, index=True
    )
    requested_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="paypal")
    provider_refund_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[BillingRefundStatus] = mapped_column(
        SAEnum(
            BillingRefundStatus,
            name="billing_refund_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=BillingRefundStatus.PENDING,
    )
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


Index("ix_billing_payments_org_created", BillingPayment.organization_id, BillingPayment.created_at)


class BillingPromotion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_promotions"

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    percent_off: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    plan_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("plans.id"), index=True)
    applies_to_existing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    applies_to_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class BillingSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "billing_settings"

    key: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    annual_savings_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    paypal_product_id: Mapped[str | None] = mapped_column(String(64))
    # Stripe credentials, entered by a platform admin in the admin panel and
    # encrypted at rest. Never returned by any API in full.
    stripe_secret_key: Mapped[str | None] = mapped_column(EncryptedString(512))
    stripe_webhook_secret: Mapped[str | None] = mapped_column(EncryptedString(512))
    # Price of one extra session / export bought as a top-up (never expire).
    session_topup_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    export_topup_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class CreditKind(str, enum.Enum):
    SESSIONS = "sessions"
    EXPORTS = "exports"


class CreditPurchase(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A one-off top-up of extra sessions or exports. Credits are added to
    the organization only once Stripe reports the checkout paid, and the
    unique checkout id makes granting idempotent across the webhook and the
    owner's return from checkout."""

    __tablename__ = "credit_purchases"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    purchased_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), nullable=False, default="stripe")
    provider_checkout_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # "pending" until paid, then "completed" (credits granted).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
