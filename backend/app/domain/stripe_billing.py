"""Stripe-backed subscriptions and top-ups for Let's Give's own SaaS fee.

This is the organization paying Let's Give for its plan -- never donor
money (see the project's scope rule: the app only reads and counts donation
notification emails).

Every state change converges on a few idempotent functions --
sync_subscription, record_invoice_payment, complete_credit_purchase -- which
both the Stripe webhook and the owner's return from Checkout call. If a
webhook is late or misconfigured, returning from Checkout still activates
the plan; if the owner closes the tab, the webhook still does.
"""
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models.billing import (
    BillingInterval,
    BillingPayment,
    BillingPaymentStatus,
    BillingSubscription,
    BillingSubscriptionStatus,
    CreditKind,
    CreditPurchase,
)
from app.db.models.organization import Organization
from app.db.models.organization import SubscriptionStatus as OrganizationSubscriptionStatus
from app.db.models.plan import Plan
from app.domain.audit import record_audit_event
from app.domain.subscriptions import (
    LIVE_SUBSCRIPTION_STATUSES,
    SubscriptionQuote,
    _has_newer_live_subscription,
    get_billing_settings,
    quote_subscription,
)
from app.integrations.stripe import StripeClient, StripeError

CURRENCY = "usd"

STRIPE_TO_LOCAL_STATUS = {
    "active": BillingSubscriptionStatus.ACTIVE,
    "trialing": BillingSubscriptionStatus.ACTIVE,
    "past_due": BillingSubscriptionStatus.PAST_DUE,
    "unpaid": BillingSubscriptionStatus.PAST_DUE,
    "paused": BillingSubscriptionStatus.SUSPENDED,
    "incomplete": BillingSubscriptionStatus.APPROVAL_PENDING,
    "incomplete_expired": BillingSubscriptionStatus.CANCELED,
    "canceled": BillingSubscriptionStatus.CANCELED,
}

STRIPE_INTERVAL = {BillingInterval.MONTHLY: "month", BillingInterval.YEARLY: "year"}
LOCAL_INTERVAL = {"month": BillingInterval.MONTHLY, "year": BillingInterval.YEARLY}


def to_cents(amount: Decimal) -> int:
    return int((Decimal(amount) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int | None) -> Decimal:
    return (Decimal(cents or 0) / Decimal(100)).quantize(Decimal("0.01"))


def _ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _id(value: Any) -> str | None:
    """Stripe fields are either an id string or an expanded object."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return str(value)


# -- customers / products --------------------------------------------------
async def ensure_customer(
    db: AsyncSession, org: Organization, stripe: StripeClient, *, email: str | None
) -> str:
    if org.stripe_customer_id:
        return org.stripe_customer_id
    customer = await stripe.create_customer(
        name=org.name,
        email=email,
        metadata={"organization_id": org.id},
        idempotency_key=f"letsgive-customer-{org.id}",
    )
    org.stripe_customer_id = customer["id"]
    await db.flush()
    return org.stripe_customer_id


async def ensure_product(db: AsyncSession, plan: Plan, stripe: StripeClient) -> str:
    if plan.stripe_product_id:
        return plan.stripe_product_id
    product = await stripe.create_product(
        name=f"Let's Give {plan.name}",
        metadata={"plan_id": plan.id, "plan_key": plan.key},
        idempotency_key=f"letsgive-product-{plan.id}",
    )
    plan.stripe_product_id = product["id"]
    await db.flush()
    return plan.stripe_product_id


def _price_data(product_id: str, quote: SubscriptionQuote) -> dict[str, Any]:
    return {
        "currency": CURRENCY,
        "product": product_id,
        "unit_amount": to_cents(quote.regular_amount),
        "recurring": {"interval": STRIPE_INTERVAL[quote.interval]},
    }


async def _promotion_coupon(stripe: StripeClient, quote: SubscriptionQuote, scope: str) -> str | None:
    """A coupon covering exactly the payments that fall inside the promotion
    window (quote.intro_cycles), then Stripe bills the regular price."""
    if quote.promotion is None or quote.intro_cycles <= 0:
        return None
    months = quote.intro_cycles * (12 if quote.interval == BillingInterval.YEARLY else 1)
    coupon = await stripe.create_coupon(
        name=quote.promotion.name,
        percent_off=quote.promotion.percent_off,
        duration_in_months=months,
        idempotency_key=f"letsgive-coupon-{quote.promotion.id}-{quote.interval.value}-{months}-{scope}",
    )
    return coupon["id"]


# -- subscription checkout / changes ----------------------------------------
async def create_subscription_checkout(
    db: AsyncSession,
    *,
    org: Organization,
    plan: Plan,
    interval: BillingInterval,
    stripe: StripeClient,
    customer_email: str | None,
    success_url: str,
    cancel_url: str,
) -> str:
    quote = await quote_subscription(
        db, plan=plan, org_discount_percent=org.discount_percent, interval=interval
    )
    customer_id = await ensure_customer(db, org, stripe, email=customer_email)
    product_id = await ensure_product(db, plan, stripe)
    coupon_id = await _promotion_coupon(stripe, quote, scope=org.id)
    metadata = {"organization_id": org.id, "plan_id": plan.id, "interval": interval.value}
    if quote.promotion is not None and coupon_id:
        metadata["promotion_id"] = quote.promotion.id
        metadata["promo_cycles"] = str(quote.intro_cycles)
    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "client_reference_id": org.id,
        "line_items": [{"price_data": _price_data(product_id, quote), "quantity": 1}],
        "subscription_data": {"metadata": metadata},
        "metadata": metadata,
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    if coupon_id:
        params["discounts"] = [{"coupon": coupon_id}]
    else:
        params["allow_promotion_codes"] = True
    session = await stripe.create_checkout_session(
        params, idempotency_key=f"letsgive-sub-{org.id}-{plan.id}-{interval.value}-{utcnow():%Y%m%d%H%M}"
    )
    return session["url"]


async def change_subscription_plan(
    db: AsyncSession,
    *,
    org: Organization,
    subscription: BillingSubscription,
    plan: Plan,
    interval: BillingInterval,
    stripe: StripeClient,
) -> BillingSubscription:
    """Moves an existing subscription to another plan or billing interval
    in place: same Stripe subscription, saved card, prorated immediately.
    Never creates a second subscription, so a switch can't double-bill."""
    remote = await stripe.retrieve_subscription(subscription.provider_subscription_id)
    items = (remote.get("items") or {}).get("data") or []
    if not items:
        raise StripeError("This subscription has no billable item to change.")
    quote = await quote_subscription(
        db, plan=plan, org_discount_percent=org.discount_percent, interval=interval
    )
    product_id = await ensure_product(db, plan, stripe)
    metadata = {"organization_id": org.id, "plan_id": plan.id, "interval": interval.value}
    updated = await stripe.update_subscription(
        subscription.provider_subscription_id,
        {
            "items": [{"id": items[0]["id"], "price_data": _price_data(product_id, quote)}],
            "proration_behavior": "always_invoice",
            # Charge the difference now; if the card fails, Stripe rejects
            # the change instead of leaving the org on an unpaid plan.
            "payment_behavior": "error_if_incomplete",
            "cancel_at_period_end": False,
            "metadata": metadata,
        },
        idempotency_key=f"letsgive-change-{subscription.id}-{plan.id}-{interval.value}-{utcnow():%Y%m%d%H%M}",
    )
    return await sync_subscription(db, updated)


async def set_cancel_at_period_end(
    db: AsyncSession, *, subscription: BillingSubscription, cancel: bool, stripe: StripeClient
) -> BillingSubscription:
    updated = await stripe.update_subscription(
        subscription.provider_subscription_id, {"cancel_at_period_end": cancel}
    )
    return await sync_subscription(db, updated)


async def apply_promotion_to_subscription(
    db: AsyncSession,
    *,
    org: Organization,
    subscription: BillingSubscription,
    quote: SubscriptionQuote,
    stripe: StripeClient,
) -> BillingSubscription:
    coupon_id = await _promotion_coupon(stripe, quote, scope=subscription.id)
    if coupon_id is None:
        raise StripeError("There is no promotion to apply right now.", status_code=400)
    updated = await stripe.update_subscription(
        subscription.provider_subscription_id,
        {
            "discounts": [{"coupon": coupon_id}],
            "metadata": {
                "promotion_id": quote.promotion.id,
                "promo_cycles": str(quote.intro_cycles),
            },
        },
    )
    return await sync_subscription(db, updated)


async def reprice_subscriptions(
    db: AsyncSession, stripe: StripeClient, *, plan_id: str | None = None, interval: BillingInterval | None = None
) -> tuple[int, int]:
    """After an admin price or yearly-saving change, moves existing Stripe
    subscriptions onto the new regular price from their next renewal (no
    proration, nothing charged now). Returns (updated, failed)."""
    query = select(BillingSubscription).where(
        BillingSubscription.provider == "stripe",
        BillingSubscription.status.in_(LIVE_SUBSCRIPTION_STATUSES),
    )
    if plan_id is not None:
        query = query.where(BillingSubscription.plan_id == plan_id)
    if interval is not None:
        query = query.where(BillingSubscription.interval == interval)
    updated = failed = 0
    for subscription in (await db.execute(query)).scalars().all():
        org = await db.get(Organization, subscription.organization_id)
        plan = await db.get(Plan, subscription.plan_id)
        if org is None or plan is None:
            continue
        quote = await quote_subscription(
            db, plan=plan, org_discount_percent=org.discount_percent, interval=subscription.interval
        )
        if subscription.regular_amount is not None and subscription.regular_amount == quote.regular_amount:
            continue
        try:
            remote = await stripe.retrieve_subscription(subscription.provider_subscription_id)
            items = (remote.get("items") or {}).get("data") or []
            product_id = await ensure_product(db, plan, stripe)
            result = await stripe.update_subscription(
                subscription.provider_subscription_id,
                {
                    "items": [{"id": items[0]["id"], "price_data": _price_data(product_id, quote)}],
                    "proration_behavior": "none",
                },
            )
            await sync_subscription(db, result)
            updated += 1
        except (StripeError, IndexError, KeyError) as exc:
            failed += 1
            await record_audit_event(
                db,
                action="stripe.reprice_failed",
                target_type="billing_subscription",
                target_id=subscription.id,
                organization_id=subscription.organization_id,
                after={"error": str(getattr(exc, "message", exc))[:500]},
            )
    return updated, failed


# -- syncing Stripe state into the database ----------------------------------
async def _org_for(db: AsyncSession, data: dict[str, Any]) -> Organization | None:
    org_id = (data.get("metadata") or {}).get("organization_id") or data.get("client_reference_id")
    if org_id:
        org = await db.get(Organization, org_id)
        if org is not None:
            return org
    customer_id = _id(data.get("customer"))
    if customer_id:
        result = await db.execute(select(Organization).where(Organization.stripe_customer_id == customer_id))
        return result.scalars().first()
    return None


async def _starter_plan(db: AsyncSession) -> Plan | None:
    return (await db.execute(select(Plan).where(Plan.key == "starter"))).scalar_one_or_none()


async def sync_subscription(db: AsyncSession, data: dict[str, Any]) -> BillingSubscription | None:
    """Upserts our copy of a Stripe subscription and applies it to the
    organization: active/past-due puts the org on the plan; an ended
    subscription drops it back to the free Starter plan (it is never locked
    out). Safe to call repeatedly with the same object."""
    org = await _org_for(db, data)
    if org is None:
        return None
    metadata = data.get("metadata") or {}
    result = await db.execute(
        select(BillingSubscription).where(BillingSubscription.provider_subscription_id == data["id"])
    )
    subscription = result.scalar_one_or_none()
    plan_id = metadata.get("plan_id") or (subscription.plan_id if subscription else None)
    if plan_id is None or await db.get(Plan, plan_id) is None:
        return subscription

    items = (data.get("items") or {}).get("data") or []
    price = (items[0].get("price") if items else None) or {}
    recurring = price.get("recurring") or {}
    interval = LOCAL_INTERVAL.get(recurring.get("interval"), BillingInterval.MONTHLY)
    regular = from_cents(price.get("unit_amount"))
    coupon = ((data.get("discount") or {}).get("coupon")) or {}
    percent_off = coupon.get("percent_off") or 0
    current_amount = (regular * (Decimal(100) - Decimal(str(percent_off))) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    if subscription is None:
        try:
            async with db.begin_nested():
                subscription = BillingSubscription(
                    organization_id=org.id,
                    plan_id=plan_id,
                    provider="stripe",
                    provider_subscription_id=data["id"],
                    currency=(price.get("currency") or CURRENCY).upper(),
                    amount=current_amount,
                )
                db.add(subscription)
        except IntegrityError:
            # The webhook and the owner's return from Checkout raced; use
            # the row the other one created.
            result = await db.execute(
                select(BillingSubscription).where(BillingSubscription.provider_subscription_id == data["id"])
            )
            subscription = result.scalar_one()
    status = STRIPE_TO_LOCAL_STATUS.get(data.get("status") or "", BillingSubscriptionStatus.APPROVAL_PENDING)
    subscription.plan_id = plan_id
    subscription.status = status
    subscription.interval = interval
    subscription.amount = current_amount
    subscription.regular_amount = regular
    subscription.currency = (price.get("currency") or CURRENCY).upper()
    subscription.current_period_start = _ts(data.get("current_period_start"))
    subscription.current_period_end = _ts(data.get("current_period_end"))
    subscription.cancel_at_period_end = bool(data.get("cancel_at_period_end"))
    subscription.canceled_at = _ts(data.get("ended_at") or data.get("canceled_at"))
    if metadata.get("promotion_id") and percent_off:
        subscription.promotion_id = metadata["promotion_id"]
        subscription.promo_cycles = int(metadata.get("promo_cycles") or 0)
    elif not percent_off:
        subscription.promotion_id = None
    if interval == BillingInterval.YEARLY:
        subscription.annual_savings_percent = (await get_billing_settings(db)).annual_savings_percent
    else:
        subscription.annual_savings_percent = None
    await db.flush()

    if await _has_newer_live_subscription(db, subscription):
        return subscription

    if status in (BillingSubscriptionStatus.ACTIVE, BillingSubscriptionStatus.PAST_DUE):
        before = {"plan_id": org.plan_id, "subscription_status": org.subscription_status.value}
        org.plan_id = plan_id
        org.subscription_status = (
            OrganizationSubscriptionStatus.ACTIVE
            if status == BillingSubscriptionStatus.ACTIVE
            else OrganizationSubscriptionStatus.PAST_DUE
        )
        org.grace_period_ends_at = None
        # A paid plan supersedes any admin-scheduled plan expiry.
        org.plan_starts_at = None
        org.plan_expires_at = None
        if before != {"plan_id": org.plan_id, "subscription_status": org.subscription_status.value}:
            await record_audit_event(
                db,
                action="stripe.subscription_applied",
                target_type="billing_subscription",
                target_id=subscription.id,
                organization_id=org.id,
                before=before,
                after={"plan_id": org.plan_id, "subscription_status": org.subscription_status.value},
            )
    elif status == BillingSubscriptionStatus.CANCELED and data.get("status") == "canceled":
        starter = await _starter_plan(db)
        if starter is not None and org.plan_id == plan_id and plan_id != starter.id:
            org.plan_id = starter.id
            org.subscription_status = OrganizationSubscriptionStatus.ACTIVE
            await record_audit_event(
                db,
                action="stripe.subscription_ended",
                target_type="billing_subscription",
                target_id=subscription.id,
                organization_id=org.id,
                after={"plan_id": starter.id, "reason": "subscription ended; moved to Starter"},
            )
    return subscription


async def record_invoice_payment(db: AsyncSession, invoice: dict[str, Any]) -> BillingPayment | None:
    """Stores a paid subscription invoice (initial payment and every
    automatic renewal). Idempotent on the invoice id."""
    if invoice.get("status") != "paid" or not invoice.get("amount_paid"):
        return None
    existing = await db.execute(
        select(BillingPayment).where(
            BillingPayment.provider == "stripe", BillingPayment.provider_payment_id == invoice["id"]
        )
    )
    if existing.scalar_one_or_none() is not None:
        return None
    subscription = None
    subscription_id = _id(invoice.get("subscription"))
    if subscription_id:
        result = await db.execute(
            select(BillingSubscription).where(BillingSubscription.provider_subscription_id == subscription_id)
        )
        subscription = result.scalar_one_or_none()
    org = await db.get(Organization, subscription.organization_id) if subscription else await _org_for(db, invoice)
    if org is None:
        return None
    lines = (invoice.get("lines") or {}).get("data") or []
    period = (lines[-1].get("period") if lines else None) or {}
    plan = await db.get(Plan, subscription.plan_id) if subscription else None
    description = (
        f"{plan.name} plan ({subscription.interval.value})" if plan and subscription else "Subscription payment"
    )
    if invoice.get("billing_reason") == "subscription_update":
        description += " — plan change"
    payment = BillingPayment(
        organization_id=org.id,
        billing_subscription_id=subscription.id if subscription else None,
        provider="stripe",
        provider_payment_id=invoice["id"],
        provider_capture_id=_id(invoice.get("payment_intent")),
        amount=from_cents(invoice.get("amount_paid")),
        currency=(invoice.get("currency") or CURRENCY).upper(),
        status=BillingPaymentStatus.COMPLETED,
        period_start=_ts(period.get("start")),
        period_end=_ts(period.get("end")),
        kind="subscription",
        description=description,
        receipt_url=invoice.get("hosted_invoice_url"),
    )
    try:
        async with db.begin_nested():
            db.add(payment)
    except IntegrityError:
        return None  # recorded concurrently by the other path
    return payment


async def create_credit_checkout(
    db: AsyncSession,
    *,
    org: Organization,
    kind: CreditKind,
    quantity: int,
    stripe: StripeClient,
    user_id: str,
    customer_email: str | None,
    success_url: str,
    cancel_url: str,
) -> str:
    settings = await get_billing_settings(db)
    unit_cents = (
        settings.session_topup_price_cents if kind == CreditKind.SESSIONS else settings.export_topup_price_cents
    )
    if unit_cents <= 0:
        raise StripeError("Top-ups are not available right now.", status_code=400)
    customer_id = await ensure_customer(db, org, stripe, email=customer_email)
    label = "extra session" if kind == CreditKind.SESSIONS else "extra export"
    metadata = {
        "organization_id": org.id,
        "purpose": "credits",
        "credit_kind": kind.value,
        "quantity": str(quantity),
    }
    session = await stripe.create_checkout_session(
        {
            "mode": "payment",
            "customer": customer_id,
            "client_reference_id": org.id,
            "line_items": [
                {
                    "price_data": {
                        "currency": CURRENCY,
                        "unit_amount": unit_cents,
                        "product_data": {
                            "name": f"Let's Give {label}",
                            "description": "Never expires. Used after your monthly allowance runs out.",
                        },
                    },
                    "quantity": quantity,
                }
            ],
            "metadata": metadata,
            "payment_intent_data": {"metadata": metadata},
            "invoice_creation": {"enabled": True},
            "success_url": success_url,
            "cancel_url": cancel_url,
        },
        idempotency_key=f"letsgive-credits-{org.id}-{kind.value}-{quantity}-{utcnow():%Y%m%d%H%M%S}",
    )
    db.add(
        CreditPurchase(
            organization_id=org.id,
            purchased_by_user_id=user_id,
            kind=kind.value,
            quantity=quantity,
            amount=from_cents(unit_cents * quantity),
            currency=CURRENCY.upper(),
            provider="stripe",
            provider_checkout_id=session["id"],
            status="pending",
        )
    )
    await db.flush()
    return session["url"]


async def complete_credit_purchase(
    db: AsyncSession, checkout: dict[str, Any], stripe: StripeClient
) -> CreditPurchase | None:
    """Grants purchased credits once Stripe says the checkout is paid.
    Idempotent: a purchase is only ever completed once."""
    if checkout.get("payment_status") != "paid":
        return None
    result = await db.execute(
        select(CreditPurchase).where(CreditPurchase.provider_checkout_id == checkout["id"])
    )
    purchase = result.scalar_one_or_none()
    metadata = checkout.get("metadata") or {}
    if purchase is None:
        # Started outside this app (or the row was lost): trust Stripe's
        # metadata, which we set ourselves when creating the checkout.
        org = await _org_for(db, checkout)
        kind = metadata.get("credit_kind")
        if org is None or kind not in {k.value for k in CreditKind}:
            return None
        purchase = CreditPurchase(
            organization_id=org.id,
            kind=kind,
            quantity=int(metadata.get("quantity") or 0),
            amount=from_cents(checkout.get("amount_total")),
            currency=(checkout.get("currency") or CURRENCY).upper(),
            provider="stripe",
            provider_checkout_id=checkout["id"],
            status="pending",
        )
        db.add(purchase)
        await db.flush()
    if purchase.status == "completed":
        return purchase

    # Claim the purchase with a conditional UPDATE so the webhook and the
    # owner's return from Checkout can never both grant the same credits.
    claimed = await db.execute(
        update(CreditPurchase)
        .where(CreditPurchase.id == purchase.id, CreditPurchase.status == "pending")
        .values(status="completed", completed_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.refresh(purchase)
    if not claimed.rowcount:
        return purchase
    org = await db.get(Organization, purchase.organization_id)
    if org is None:
        return None
    # Atomic increment: a session being created at the same moment may be
    # spending a credit, and a read-modify-write here could undo that.
    attribute = "session_credits" if purchase.kind == CreditKind.SESSIONS.value else "export_credits"
    column = getattr(Organization, attribute)
    await db.execute(
        update(Organization)
        .where(Organization.id == org.id)
        .values({attribute: column + purchase.quantity})
        .execution_options(synchronize_session=False)
    )
    await db.refresh(org, attribute_names=[attribute])

    payment_intent_id = _id(checkout.get("payment_intent"))
    receipt_url = None
    if payment_intent_id:
        try:
            intent = await stripe.retrieve_payment_intent(payment_intent_id)
            charge = intent.get("latest_charge")
            receipt_url = charge.get("receipt_url") if isinstance(charge, dict) else None
        except StripeError:
            receipt_url = None  # the receipt link is a nicety; the credits already stand
    label = "extra sessions" if purchase.kind == CreditKind.SESSIONS.value else "extra exports"
    existing_payment = await db.execute(
        select(BillingPayment).where(
            BillingPayment.provider == "stripe",
            BillingPayment.provider_payment_id == (payment_intent_id or checkout["id"]),
        )
    )
    if existing_payment.scalar_one_or_none() is None:
        payment = (
            BillingPayment(
                organization_id=org.id,
                provider="stripe",
                provider_payment_id=payment_intent_id or checkout["id"],
                provider_capture_id=payment_intent_id,
                amount=from_cents(checkout.get("amount_total")) if checkout.get("amount_total") else purchase.amount,
                currency=purchase.currency,
                status=BillingPaymentStatus.COMPLETED,
                kind="session_credits" if purchase.kind == CreditKind.SESSIONS.value else "export_credits",
                description=f"{purchase.quantity} {label}",
                receipt_url=receipt_url,
            )
        )
        try:
            async with db.begin_nested():
                db.add(payment)
        except IntegrityError:
            pass
    await record_audit_event(
        db,
        action="stripe.credits_purchased",
        target_type="credit_purchase",
        target_id=purchase.id,
        organization_id=org.id,
        actor_user_id=purchase.purchased_by_user_id,
        after={"kind": purchase.kind, "quantity": purchase.quantity, "amount": str(purchase.amount)},
    )
    await db.flush()
    return purchase


async def process_checkout_session(
    db: AsyncSession, checkout: dict[str, Any], stripe: StripeClient
) -> None:
    if checkout.get("mode") == "subscription":
        subscription_id = _id(checkout.get("subscription"))
        if subscription_id and checkout.get("status") == "complete":
            await sync_subscription(db, await stripe.retrieve_subscription(subscription_id))
            invoice_id = _id(checkout.get("invoice"))
            if invoice_id:
                await record_invoice_payment(db, await stripe.retrieve_invoice(invoice_id))
    elif checkout.get("mode") == "payment" and (checkout.get("metadata") or {}).get("purpose") == "credits":
        await complete_credit_purchase(db, checkout, stripe)


async def handle_stripe_event(db: AsyncSession, event: dict[str, Any], stripe: StripeClient) -> None:
    """Applies one verified webhook event. Objects are re-fetched from the
    API rather than trusted from the payload (see app/integrations/stripe.py
    for why)."""
    from app.domain.billing import _notify_billing_contacts

    event_type = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    object_id = obj.get("id")
    if not object_id:
        return

    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        await process_checkout_session(db, await stripe.retrieve_checkout_session(object_id), stripe)
    elif event_type.startswith("customer.subscription."):
        await sync_subscription(db, await stripe.retrieve_subscription(object_id))
    elif event_type == "invoice.paid":
        invoice = await stripe.retrieve_invoice(object_id)
        subscription_id = _id(invoice.get("subscription"))
        if subscription_id:
            await sync_subscription(db, await stripe.retrieve_subscription(subscription_id))
        await record_invoice_payment(db, invoice)
    elif event_type == "invoice.payment_failed":
        invoice = await stripe.retrieve_invoice(object_id)
        subscription_id = _id(invoice.get("subscription"))
        if not subscription_id:
            return
        subscription = await sync_subscription(db, await stripe.retrieve_subscription(subscription_id))
        if subscription is not None:
            org = await db.get(Organization, subscription.organization_id)
            if org is not None:
                await _notify_billing_contacts(
                    db,
                    org,
                    title="Subscription payment failed",
                    body=(
                        "We couldn't charge the card on file for your Let's Give subscription. "
                        "Stripe will retry automatically; to avoid interruption, update your card "
                        "from the Billing page (Manage payment method)."
                    ),
                )
