from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc, utcnow
from app.db.models.billing import (
    BillingPayment,
    BillingPaymentStatus,
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


def plan_paypal_id(plan: Plan, mapping: str) -> str | None:
    by_key = {}
    for part in mapping.split(","):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        by_key[key.strip()] = value.strip()
    return by_key.get(plan.key) or plan.paypal_plan_id


def next_month_same_day(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


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


async def apply_paypal_webhook(db: AsyncSession, event: dict[str, Any]) -> None:
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
        billing_subscription.status = BillingSubscriptionStatus.ACTIVE
        org.plan_id = billing_subscription.plan_id
        org.subscription_status = OrganizationSubscriptionStatus.ACTIVE
        org.grace_period_ends_at = None
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
        billing_subscription.canceled_at = utcnow()
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
        billing_subscription.status = BillingSubscriptionStatus.PAST_DUE
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
        period_end = next_month_same_day(period_start)
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
