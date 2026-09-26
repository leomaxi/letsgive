from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc, utcnow
from app.db.models.billing import (
    BillingInterval,
    BillingPayment,
    BillingPaymentStatus,
    BillingPromotion,
    BillingSettings,
    BillingRefund,
    BillingRefundStatus,
    BillingSubscription,
    BillingSubscriptionStatus,
)
from app.db.models.organization import Organization
from app.db.models.organization import SubscriptionStatus as OrganizationSubscriptionStatus
from app.db.models.plan import Plan
from app.domain.audit import record_audit_event
from app.integrations.paypal import PayPalClient


def price_from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def discounted_price_from_cents(cents: int, discount_percent: int) -> Decimal:
    discount = min(max(discount_percent, 0), 100)
    amount_cents = Decimal(cents) * (Decimal(100 - discount) / Decimal(100))
    return (amount_cents / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def get_billing_settings(db: AsyncSession) -> BillingSettings:
    query = select(BillingSettings).where(BillingSettings.key == "default")
    settings = (await db.execute(query)).scalar_one_or_none()
    if settings is not None:
        return settings
    # Migration 0020 seeds this row; the fallback covers fresh test schemas.
    # Concurrent requests can race to create it, so insert in a savepoint
    # and re-read whichever row won.
    try:
        async with db.begin_nested():
            db.add(BillingSettings(key="default", annual_savings_percent=8))
    except IntegrityError:
        pass
    return (await db.execute(query)).scalar_one()


async def active_promotion_for_plan(
    db: AsyncSession,
    plan_id: str,
    *,
    existing_subscription: bool = False,
    now: datetime | None = None,
) -> BillingPromotion | None:
    current = now or utcnow()
    result = await db.execute(
        select(BillingPromotion)
        .where(
            BillingPromotion.is_active.is_(True),
            BillingPromotion.starts_at <= current,
            BillingPromotion.ends_at >= current,
            (BillingPromotion.plan_id.is_(None)) | (BillingPromotion.plan_id == plan_id),
        )
        .order_by(BillingPromotion.percent_off.desc(), BillingPromotion.created_at.desc())
    )
    for promotion in result.scalars().all():
        if existing_subscription and promotion.applies_to_existing:
            return promotion
        if not existing_subscription and promotion.applies_to_new:
            return promotion
    return None


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_month_same_day(value: datetime) -> datetime:
    return add_months(value, 1)


def next_billing_date(value: datetime, interval: BillingInterval) -> datetime:
    return add_months(value, 12 if interval == BillingInterval.YEARLY else 1)


def promo_cycle_count(
    first_charge_at: datetime, promotion: BillingPromotion, interval: BillingInterval
) -> int:
    """Number of consecutive charges, starting at first_charge_at, that fall
    inside the promotion window. Each of those charges is billed at the
    promotional price; charges after the window revert to the regular price."""
    start = ensure_utc(promotion.starts_at)
    end = ensure_utc(promotion.ends_at)
    charge_at = ensure_utc(first_charge_at)
    cycles = 0
    while start <= charge_at <= end and cycles < 240:
        cycles += 1
        charge_at = next_billing_date(charge_at, interval)
    return cycles


def _cents_to_amount(cents: Decimal) -> Decimal:
    return (cents / Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent_off(cents: Decimal, percent: int) -> Decimal:
    percent = min(max(percent, 0), 100)
    return cents * (Decimal(100 - percent) / Decimal(100))


def list_price_cents(plan: Plan, interval: BillingInterval, annual_savings_percent: int) -> Decimal:
    """Plan price for one billing cycle before org discounts or promotions."""
    cents = Decimal(plan.monthly_price_cents)
    if interval == BillingInterval.YEARLY:
        return _percent_off(cents * 12, annual_savings_percent)
    return cents


@dataclass
class SubscriptionQuote:
    interval: BillingInterval
    # What 12 monthly payments would cost (yearly) or one month (monthly),
    # before org discount / promotion -- used to show the yearly saving.
    undiscounted_amount: Decimal
    regular_amount: Decimal
    intro_amount: Decimal
    intro_cycles: int
    annual_savings_percent: int | None
    promotion: BillingPromotion | None


async def quote_subscription(
    db: AsyncSession,
    *,
    plan: Plan,
    org_discount_percent: int,
    interval: BillingInterval,
    existing_subscription: bool = False,
    first_charge_at: datetime | None = None,
) -> SubscriptionQuote:
    settings = await get_billing_settings(db)
    savings = settings.annual_savings_percent if interval == BillingInterval.YEARLY else None
    base_cents = list_price_cents(plan, interval, savings or 0)
    regular_cents = _percent_off(base_cents, org_discount_percent)
    undiscounted = Decimal(plan.monthly_price_cents) * (12 if interval == BillingInterval.YEARLY else 1)

    first_charge = first_charge_at or utcnow()
    promotion = await active_promotion_for_plan(
        db, plan.id, existing_subscription=existing_subscription
    )
    intro_cycles = promo_cycle_count(first_charge, promotion, interval) if promotion else 0
    if intro_cycles == 0:
        promotion = None
    intro_cents = _percent_off(regular_cents, promotion.percent_off) if promotion else regular_cents

    return SubscriptionQuote(
        interval=interval,
        undiscounted_amount=_cents_to_amount(undiscounted),
        regular_amount=_cents_to_amount(regular_cents),
        intro_amount=_cents_to_amount(intro_cents),
        intro_cycles=intro_cycles,
        annual_savings_percent=savings,
        promotion=promotion,
    )


async def ensure_paypal_plan_id(
    db: AsyncSession, *, plan: Plan, interval: BillingInterval, paypal: PayPalClient
) -> str:
    """Returns the PayPal billing plan for this plan + interval, creating the
    PayPal product and plan on first use. Per-subscription pricing is always
    sent as an override, so the price baked into the PayPal plan only matters
    as a default and does not need updating when an admin changes prices."""
    attr = "paypal_yearly_plan_id" if interval == BillingInterval.YEARLY else "paypal_monthly_plan_id"
    existing = getattr(plan, attr)
    if existing:
        return existing

    settings = await get_billing_settings(db)
    if not settings.paypal_product_id:
        product = await paypal.create_product(
            name="Let's Give subscription", request_id=f"letsgive-product-{settings.id}"
        )
        settings.paypal_product_id = str(product["id"])

    savings = settings.annual_savings_percent if interval == BillingInterval.YEARLY else 0
    created = await paypal.create_plan(
        product_id=settings.paypal_product_id,
        name=f"Let's Give {plan.name} ({interval.value})",
        interval=interval.value,
        price=_cents_to_amount(list_price_cents(plan, interval, savings)),
        currency="USD",
        request_id=f"letsgive-plan-{plan.id}-{interval.value}",
    )
    setattr(plan, attr, str(created["id"]))
    await db.flush()
    return getattr(plan, attr)


def prorated_unused_amount(payment: BillingPayment, now: datetime | None = None) -> Decimal:
    if payment.period_start is None or payment.period_end is None:
        return Decimal("0.00")
    current = ensure_utc(now or utcnow())
    start = ensure_utc(payment.period_start)
    end = ensure_utc(payment.period_end)
    if current >= end:
        return Decimal("0.00")
    if current <= start:
        return Decimal(payment.amount).quantize(Decimal("0.01"))
    total_seconds = Decimal(str((end - start).total_seconds()))
    unused_seconds = Decimal(str((end - current).total_seconds()))
    if total_seconds <= 0:
        return Decimal("0.00")
    amount = Decimal(payment.amount) * (unused_seconds / total_seconds)
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def latest_refundable_payment(
    db: AsyncSession, organization_id: str
) -> BillingPayment | None:
    result = await db.execute(
        select(BillingPayment)
        .where(
            BillingPayment.organization_id == organization_id,
            BillingPayment.status != BillingPaymentStatus.REFUNDED,
            BillingPayment.provider_capture_id.is_not(None),
        )
        .order_by(BillingPayment.created_at.desc())
    )
    return result.scalars().first()


async def create_prorated_refund(
    db: AsyncSession,
    *,
    organization_id: str,
    requested_by_user_id: str,
    reason: str,
    paypal: PayPalClient,
) -> BillingRefund:
    reason = reason.strip()
    if not reason:
        raise ValueError("Refund reason is required.")

    payment = await latest_refundable_payment(db, organization_id)
    if payment is None or payment.provider_capture_id is None:
        raise ValueError("No refundable PayPal payment was found for this organization.")

    amount = prorated_unused_amount(payment)
    if amount <= 0:
        raise ValueError("There are no unused subscription days left to refund.")

    refund = BillingRefund(
        organization_id=organization_id,
        billing_payment_id=payment.id,
        requested_by_user_id=requested_by_user_id,
        amount=amount,
        currency=payment.currency,
        reason=reason,
        status=BillingRefundStatus.PENDING,
    )
    db.add(refund)
    await db.flush()

    response = await paypal.refund_capture(
        capture_id=payment.provider_capture_id,
        amount=amount,
        currency=payment.currency,
        note=reason,
        request_id=f"refund-{refund.id}",
    )
    refund.provider_refund_id = response.get("id")
    refund.status = (
        BillingRefundStatus.COMPLETED
        if response.get("status") in {None, "COMPLETED", "PENDING"}
        else BillingRefundStatus.FAILED
    )
    refund.processed_at = utcnow()
    payment.status = (
        BillingPaymentStatus.REFUNDED if amount >= payment.amount else BillingPaymentStatus.PARTIALLY_REFUNDED
    )
    return refund


def _paypal_money(resource: dict[str, Any]) -> tuple[Decimal, str]:
    amount = resource.get("amount") or {}
    value = amount.get("total") or amount.get("value") or "0.00"
    currency = amount.get("currency") or amount.get("currency_code") or "CAD"
    return Decimal(str(value)).quantize(Decimal("0.01")), str(currency).upper()


def _paypal_subscription_id(resource: dict[str, Any]) -> str | None:
    return (
        resource.get("billing_agreement_id")
        or resource.get("subscription_id")
        or resource.get("id")
    )


def _paypal_capture_id(resource: dict[str, Any]) -> str | None:
    related_ids = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    return related_ids.get("capture_id") or resource.get("capture_id") or resource.get("id")


LIVE_SUBSCRIPTION_STATUSES = (
    BillingSubscriptionStatus.ACTIVE,
    BillingSubscriptionStatus.PAST_DUE,
    BillingSubscriptionStatus.SUSPENDED,
)


async def _has_newer_live_subscription(db: AsyncSession, subscription: BillingSubscription) -> bool:
    """True when the org has moved on to a later subscription (plan or
    billing-interval switch). Webhooks for the superseded one must then only
    update its own row, never the organization's plan or status."""
    result = await db.execute(
        select(BillingSubscription.id).where(
            BillingSubscription.organization_id == subscription.organization_id,
            BillingSubscription.id != subscription.id,
            BillingSubscription.created_at > subscription.created_at,
            BillingSubscription.status.in_(LIVE_SUBSCRIPTION_STATUSES),
        )
    )
    return result.first() is not None


async def _cancel_superseded_subscription(
    db: AsyncSession,
    subscription: BillingSubscription,
    *,
    replaced_by: BillingSubscription,
    paypal: PayPalClient | None,
) -> None:
    """Stops PayPal billing an org's previous subscription once a new one
    activates, so a plan or interval switch never charges twice. A PayPal
    failure is audited and the row left live, so it stays visible to admins
    rather than silently marked canceled while PayPal keeps charging."""
    try:
        if paypal is None:
            raise RuntimeError("No PayPal client available to cancel the subscription.")
        await paypal.cancel_subscription(
            subscription.provider_subscription_id, "Replaced by a new Let's Give subscription."
        )
    except Exception as exc:  # noqa: BLE001 -- activation must still succeed
        await record_audit_event(
            db,
            action="paypal.superseded_subscription_cancel_failed",
            target_type="billing_subscription",
            target_id=subscription.id,
            organization_id=subscription.organization_id,
            after={
                "paypal_subscription_id": subscription.provider_subscription_id,
                "replaced_by": replaced_by.id,
                "error": str(getattr(exc, "detail", exc))[:500],
            },
        )
        return
    subscription.status = BillingSubscriptionStatus.CANCELED
    subscription.canceled_at = utcnow()
    await record_audit_event(
        db,
        action="paypal.subscription_superseded",
        target_type="billing_subscription",
        target_id=subscription.id,
        organization_id=subscription.organization_id,
        after={
            "paypal_subscription_id": subscription.provider_subscription_id,
            "replaced_by": replaced_by.id,
        },
    )


async def apply_paypal_webhook(
    db: AsyncSession, event: dict[str, Any], paypal: PayPalClient | None = None
) -> None:
    event_type = event.get("event_type")
    resource = event.get("resource") or {}

    if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
        provider_subscription_id = resource.get("id")
        if not provider_subscription_id:
            return
        result = await db.execute(
            select(BillingSubscription).where(
                BillingSubscription.provider_subscription_id == provider_subscription_id
            )
        )
        billing_subscription = result.scalar_one_or_none()
        if billing_subscription is None:
            return
        org = await db.get(Organization, billing_subscription.organization_id)
        if org is None:
            return
        if await _has_newer_live_subscription(db, billing_subscription):
            # An older checkout approved late, after the org already moved to
            # a newer subscription: cancel this one instead of reverting.
            billing_subscription.status = BillingSubscriptionStatus.ACTIVE
            newer = (
                await db.execute(
                    select(BillingSubscription)
                    .where(
                        BillingSubscription.organization_id == org.id,
                        BillingSubscription.status.in_(LIVE_SUBSCRIPTION_STATUSES),
                        BillingSubscription.created_at > billing_subscription.created_at,
                    )
                    .order_by(BillingSubscription.created_at.desc())
                )
            ).scalars().first()
            await _cancel_superseded_subscription(
                db, billing_subscription, replaced_by=newer, paypal=paypal
            )
            return
        billing_subscription.status = BillingSubscriptionStatus.ACTIVE
        org.plan_id = billing_subscription.plan_id
        org.subscription_status = OrganizationSubscriptionStatus.ACTIVE
        org.grace_period_ends_at = None
        older = (
            await db.execute(
                select(BillingSubscription).where(
                    BillingSubscription.organization_id == org.id,
                    BillingSubscription.id != billing_subscription.id,
                    BillingSubscription.status.in_(LIVE_SUBSCRIPTION_STATUSES),
                )
            )
        ).scalars().all()
        for previous in older:
            await _cancel_superseded_subscription(
                db, previous, replaced_by=billing_subscription, paypal=paypal
            )
        await record_audit_event(
            db,
            action="paypal.subscription_activated",
            target_type="billing_subscription",
            target_id=billing_subscription.id,
            organization_id=org.id,
            after={"paypal_subscription_id": provider_subscription_id},
        )
        return

    if event_type == "BILLING.SUBSCRIPTION.CANCELLED":
        provider_subscription_id = resource.get("id")
        if not provider_subscription_id:
            return
        result = await db.execute(
            select(BillingSubscription).where(
                BillingSubscription.provider_subscription_id == provider_subscription_id
            )
        )
        billing_subscription = result.scalar_one_or_none()
        if billing_subscription is None:
            return
        org = await db.get(Organization, billing_subscription.organization_id)
        if org is None:
            return
        billing_subscription.status = BillingSubscriptionStatus.CANCELED
        billing_subscription.canceled_at = billing_subscription.canceled_at or utcnow()
        if await _has_newer_live_subscription(db, billing_subscription):
            # Expected echo of _cancel_superseded_subscription: the org is on
            # its newer subscription and must not be flipped to canceled.
            return
        org.subscription_status = OrganizationSubscriptionStatus.CANCELED
        org.grace_period_ends_at = utcnow() + timedelta(days=30)
        await record_audit_event(
            db,
            action="paypal.subscription_canceled",
            target_type="billing_subscription",
            target_id=billing_subscription.id,
            organization_id=org.id,
            after={"paypal_subscription_id": provider_subscription_id},
        )
        return

    if event_type == "BILLING.SUBSCRIPTION.PAYMENT.FAILED":
        provider_subscription_id = resource.get("id") or resource.get("billing_agreement_id")
        if not provider_subscription_id:
            return
        result = await db.execute(
            select(BillingSubscription).where(
                BillingSubscription.provider_subscription_id == provider_subscription_id
            )
        )
        billing_subscription = result.scalar_one_or_none()
        if billing_subscription is None:
            return
        org = await db.get(Organization, billing_subscription.organization_id)
        if org is None:
            return
        if billing_subscription.status == BillingSubscriptionStatus.CANCELED:
            return
        billing_subscription.status = BillingSubscriptionStatus.PAST_DUE
        if not await _has_newer_live_subscription(db, billing_subscription):
            org.subscription_status = OrganizationSubscriptionStatus.PAST_DUE
        return

    if event_type == "PAYMENT.SALE.COMPLETED":
        provider_payment_id = resource.get("id")
        provider_subscription_id = _paypal_subscription_id(resource)
        if not provider_payment_id or not provider_subscription_id:
            return
        existing = await db.execute(
            select(BillingPayment).where(
                BillingPayment.provider == "paypal",
                BillingPayment.provider_payment_id == provider_payment_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            return
        result = await db.execute(
            select(BillingSubscription).where(
                BillingSubscription.provider_subscription_id == provider_subscription_id
            )
        )
        billing_subscription = result.scalar_one_or_none()
        if billing_subscription is None:
            return
        amount, currency = _paypal_money(resource)
        period_start = utcnow()
        period_end = next_billing_date(period_start, billing_subscription.interval)
        payment = BillingPayment(
            organization_id=billing_subscription.organization_id,
            billing_subscription_id=billing_subscription.id,
            provider_payment_id=provider_payment_id,
            provider_capture_id=_paypal_capture_id(resource),
            amount=amount,
            currency=currency,
            status=BillingPaymentStatus.COMPLETED,
            period_start=period_start,
            period_end=period_end,
        )
        db.add(payment)
        # A payment on a superseded (or already canceled) subscription is
        # still recorded, but must not move the org back onto its old plan.
        if billing_subscription.status == BillingSubscriptionStatus.CANCELED:
            return
        if await _has_newer_live_subscription(db, billing_subscription):
            return
        billing_subscription.current_period_start = period_start
        billing_subscription.current_period_end = period_end
        billing_subscription.status = BillingSubscriptionStatus.ACTIVE
        org = await db.get(Organization, billing_subscription.organization_id)
        if org is not None:
            org.subscription_status = OrganizationSubscriptionStatus.ACTIVE
            org.plan_id = billing_subscription.plan_id
        return

    if event_type == "PAYMENT.SALE.REFUNDED":
        provider_refund_id = resource.get("id")
        sale_id = resource.get("sale_id") or resource.get("parent_payment")
        if not provider_refund_id or not sale_id:
            return
        result = await db.execute(
            select(BillingPayment).where(BillingPayment.provider_payment_id == sale_id)
        )
        payment = result.scalar_one_or_none()
        if payment is not None:
            payment.status = BillingPaymentStatus.PARTIALLY_REFUNDED


async def revert_expired_plans(db: AsyncSession) -> int:
    """Reverts any organization whose admin-assigned plan_expires_at has
    passed back to the Starter plan (spec: a platform admin can grant a
    tenant a plan for a limited date range; if nobody renews it, it lapses
    rather than silently continuing forever). Called by the background loop
    in app/main.py, same shape as poll_all_imap_connections. Returns the
    number of organizations reverted, mostly useful for tests.
    """
    starter = (await db.execute(select(Plan).where(Plan.key == "starter"))).scalar_one_or_none()
    if starter is None:
        # Seed data missing (shouldn't happen outside a broken local setup) --
        # nothing sane to revert to, so leave expired grants alone rather than
        # nulling out plan_id and breaking entitlement checks outright.
        return 0

    now = utcnow()
    result = await db.execute(select(Organization).where(Organization.plan_expires_at.is_not(None)))
    reverted = 0
    for org in result.scalars().all():
        if ensure_utc(org.plan_expires_at) > now:
            continue
        before = {"plan_id": org.plan_id, "plan_expires_at": org.plan_expires_at.isoformat()}
        org.plan_id = starter.id
        org.plan_starts_at = None
        org.plan_expires_at = None
        await record_audit_event(
            db,
            action="subscription.plan_expired",
            target_type="organization",
            target_id=org.id,
            organization_id=org.id,
            actor_user_id=None,
            before=before,
            after={"plan_id": starter.id},
        )
        reverted += 1

    if reverted:
        await db.commit()
    return reverted
