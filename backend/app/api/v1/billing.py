"""Tenant-facing billing: Let's Give's own subscription fee, paid by the
organization through Stripe (never donor money)."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    BillingOverviewOut,
    BillingPaymentOut,
    BillingPricingOut,
    BillingRefundOut,
    BillingSubscriptionOut,
    CheckoutConfirmRequest,
    CheckoutOut,
    CheckoutRequest,
    PlanPriceQuoteOut,
    PromotionSummaryOut,
    RedirectOut,
    TopUpRequest,
    UsageOut,
)
from app.core.config import get_settings
from app.db.base import ensure_utc, utcnow
from app.db.models.billing import (
    BillingInterval,
    BillingPayment,
    BillingRefund,
    BillingSubscription,
    BillingSubscriptionStatus,
)
from app.db.models.membership import Membership, Role
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.billing import usage_summary
from app.domain.rbac import require_roles
from app.domain.stripe_billing import (
    apply_promotion_to_subscription,
    change_subscription_plan,
    create_credit_checkout,
    create_subscription_checkout,
    process_checkout_session,
    set_cancel_at_period_end,
)
from app.domain.subscriptions import (
    LIVE_SUBSCRIPTION_STATUSES,
    SubscriptionQuote,
    get_billing_settings,
    latest_refundable_payment,
    prorated_unused_amount,
    quote_subscription,
)
from app.integrations.stripe import StripeClient, StripeError, get_stripe_client

router = APIRouter(prefix="/v1/organizations/{organization_id}/billing", tags=["billing"])


def site_url(request: Request) -> str:
    """Base URL of the site the owner is using, for Stripe's return links.
    The API has no CORS, so an authenticated call's Origin is always the
    app's own site; LETSGIVE_PUBLIC_APP_URL overrides it when set."""
    configured = get_settings().public_app_url
    if configured:
        return configured.rstrip("/")
    origin = request.headers.get("origin")
    return origin.rstrip("/") if origin else str(request.base_url).rstrip("/")


def _stripe_http_error(exc: StripeError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _require_billing_access(membership: Membership) -> None:
    if membership.role not in {Role.OWNER, Role.FINANCE}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Billing access required.")


async def current_subscription(db: AsyncSession, organization_id: str) -> BillingSubscription | None:
    """The org's subscription as far as the owner is concerned. A checkout
    that never completed (APPROVAL_PENDING) is not a subscription and must
    not hide the real one or make a free plan look billed."""
    result = await db.execute(
        select(BillingSubscription)
        .where(
            BillingSubscription.organization_id == organization_id,
            BillingSubscription.status != BillingSubscriptionStatus.APPROVAL_PENDING,
        )
        .order_by(BillingSubscription.created_at.desc())
    )
    return result.scalars().first()


async def live_stripe_subscription(db: AsyncSession, organization_id: str) -> BillingSubscription | None:
    subscription = await current_subscription(db, organization_id)
    if (
        subscription is not None
        and subscription.provider == "stripe"
        and subscription.status in LIVE_SUBSCRIPTION_STATUSES
    ):
        return subscription
    return None


def _quote_out(plan_id: str, quote: SubscriptionQuote) -> PlanPriceQuoteOut:
    return PlanPriceQuoteOut(
        plan_id=plan_id,
        interval=quote.interval,
        undiscounted_amount=quote.undiscounted_amount,
        regular_amount=quote.regular_amount,
        intro_amount=quote.intro_amount,
        intro_cycles=quote.intro_cycles,
        savings_amount=max(quote.undiscounted_amount - quote.regular_amount, Decimal("0.00")),
        annual_savings_percent=quote.annual_savings_percent,
        promotion=PromotionSummaryOut.model_validate(quote.promotion) if quote.promotion else None,
    )


async def _claimable_promotion(
    db: AsyncSession, org: Organization, subscription: BillingSubscription | None
) -> SubscriptionQuote | None:
    """A running promotion for existing subscribers that this subscription
    hasn't claimed yet and whose window covers its next renewal."""
    if (
        subscription is None
        or subscription.provider != "stripe"
        or subscription.status != BillingSubscriptionStatus.ACTIVE
        or subscription.cancel_at_period_end
    ):
        return None
    plan = await db.get(Plan, subscription.plan_id)
    if plan is None or plan.monthly_price_cents <= 0:
        return None
    next_charge_at = subscription.current_period_end or utcnow()
    if ensure_utc(next_charge_at) < utcnow():
        next_charge_at = utcnow()
    quote = await quote_subscription(
        db,
        plan=plan,
        org_discount_percent=org.discount_percent,
        interval=subscription.interval,
        existing_subscription=True,
        first_charge_at=next_charge_at,
    )
    if quote.promotion is None or quote.intro_cycles <= 0 or subscription.promotion_id == quote.promotion.id:
        return None
    return quote


async def build_overview(db: AsyncSession, organization_id: str) -> BillingOverviewOut:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    subscription = await current_subscription(db, organization_id)
    payments = (
        await db.execute(
            select(BillingPayment)
            .where(BillingPayment.organization_id == organization_id)
            .order_by(BillingPayment.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    refunds = (
        await db.execute(
            select(BillingRefund)
            .where(BillingRefund.organization_id == organization_id)
            .order_by(BillingRefund.created_at.desc())
        )
    ).scalars().all()
    refundable_payment = await latest_refundable_payment(db, organization_id)
    settings = await get_billing_settings(db)
    usage = await usage_summary(db, org)
    promotion = await _claimable_promotion(db, org, subscription)
    return BillingOverviewOut(
        subscription=BillingSubscriptionOut.model_validate(subscription) if subscription else None,
        payments=[BillingPaymentOut.model_validate(payment) for payment in payments],
        refunds=[BillingRefundOut.model_validate(refund) for refund in refunds],
        refundable_amount=(
            prorated_unused_amount(refundable_payment) if refundable_payment is not None else Decimal("0.00")
        ),
        refundable_payment_id=refundable_payment.id if refundable_payment else None,
        usage=UsageOut(**usage.__dict__),
        payments_enabled=bool(settings.stripe_secret_key),
        session_topup_price_cents=settings.session_topup_price_cents,
        export_topup_price_cents=settings.export_topup_price_cents,
        available_promotion=_quote_out(subscription.plan_id, promotion) if promotion and subscription else None,
    )


@router.get("/pricing", response_model=BillingPricingOut)
async def get_billing_pricing(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> BillingPricingOut:
    _require_billing_access(membership)
    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    settings = await get_billing_settings(db)
    plans = (await db.execute(select(Plan).order_by(Plan.monthly_price_cents))).scalars().all()
    quotes: list[PlanPriceQuoteOut] = []
    for plan in plans:
        for interval in BillingInterval:
            quote = await quote_subscription(
                db, plan=plan, org_discount_percent=org.discount_percent, interval=interval
            )
            quotes.append(_quote_out(plan.id, quote))
    await db.commit()
    return BillingPricingOut(annual_savings_percent=settings.annual_savings_percent, quotes=quotes)


@router.get("", response_model=BillingOverviewOut)
async def get_billing_overview(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> BillingOverviewOut:
    _require_billing_access(membership)
    return await build_overview(db, membership.organization_id)


@router.post("/checkout", response_model=CheckoutOut)
async def start_checkout(
    payload: CheckoutRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> CheckoutOut:
    """Subscribe to a paid plan, or change plan / billing interval.

    No subscription yet -> a Stripe Checkout link. Existing subscription ->
    changed in place with the saved card (prorated), so switching never
    creates a second subscription or double-bills."""
    require_roles(membership, Role.OWNER)
    org = await db.get(Organization, membership.organization_id)
    plan = await db.get(Plan, payload.plan_id)
    if org is None or plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")
    if plan.monthly_price_cents <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The free plan doesn't need payment. To move to it, cancel your paid subscription; "
            "you keep your current plan until the end of the period you've paid for.",
        )
    stripe.require_configured()

    try:
        existing = await live_stripe_subscription(db, org.id)
        if existing is not None:
            if existing.plan_id == plan.id and existing.interval == payload.interval:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="You're already on this plan."
                )
            before = {"plan_id": existing.plan_id, "interval": existing.interval.value}
            await change_subscription_plan(
                db, org=org, subscription=existing, plan=plan, interval=payload.interval, stripe=stripe
            )
            await record_audit_event(
                db,
                action="billing.plan_changed",
                target_type="billing_subscription",
                target_id=existing.id,
                organization_id=org.id,
                actor_user_id=current_user.id,
                before=before,
                after={"plan_id": plan.id, "interval": payload.interval.value},
                ip_address=request.client.host if request.client else None,
            )
            await db.commit()
            return CheckoutOut(action="updated", overview=await build_overview(db, org.id))

        base = site_url(request)
        url = await create_subscription_checkout(
            db,
            org=org,
            plan=plan,
            interval=payload.interval,
            stripe=stripe,
            customer_email=current_user.email,
            success_url=f"{base}/billing?stripe=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing?stripe=canceled",
        )
    except StripeError as exc:
        await db.rollback()
        raise _stripe_http_error(exc) from exc
    await db.commit()
    return CheckoutOut(action="redirect", url=url)


@router.post("/checkout/confirm", response_model=BillingOverviewOut)
async def confirm_checkout(
    payload: CheckoutConfirmRequest,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> BillingOverviewOut:
    """Called when the owner lands back from Stripe Checkout. Reads the
    result from Stripe itself (never trusts the redirect), so the plan or
    credits apply immediately even if the webhook is slow."""
    _require_billing_access(membership)
    try:
        checkout = await stripe.retrieve_checkout_session(payload.session_id)
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc
    if checkout.get("client_reference_id") != membership.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Checkout not found.")
    try:
        await process_checkout_session(db, checkout, stripe)
    except StripeError as exc:
        await db.rollback()
        raise _stripe_http_error(exc) from exc
    await db.commit()
    return await build_overview(db, membership.organization_id)


@router.post("/cancel", response_model=BillingOverviewOut)
async def cancel_auto_renew(
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> BillingOverviewOut:
    """Stops auto-renew. The plan stays until the end of the paid period,
    then the organization moves to the free Starter plan."""
    require_roles(membership, Role.OWNER)
    subscription = await live_stripe_subscription(db, membership.organization_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="There's no paid subscription to cancel.")
    try:
        await set_cancel_at_period_end(db, subscription=subscription, cancel=True, stripe=stripe)
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc
    await record_audit_event(
        db,
        action="billing.auto_renew_canceled",
        target_type="billing_subscription",
        target_id=subscription.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        after={"ends_at": subscription.current_period_end.isoformat() if subscription.current_period_end else None},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return await build_overview(db, membership.organization_id)


@router.post("/resume", response_model=BillingOverviewOut)
async def resume_auto_renew(
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> BillingOverviewOut:
    require_roles(membership, Role.OWNER)
    subscription = await live_stripe_subscription(db, membership.organization_id)
    if subscription is None or not subscription.cancel_at_period_end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Auto-renew is already on.")
    try:
        await set_cancel_at_period_end(db, subscription=subscription, cancel=False, stripe=stripe)
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc
    await record_audit_event(
        db,
        action="billing.auto_renew_resumed",
        target_type="billing_subscription",
        target_id=subscription.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return await build_overview(db, membership.organization_id)


@router.post("/portal", response_model=RedirectOut)
async def open_billing_portal(
    request: Request,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> RedirectOut:
    """Stripe's hosted page for updating the card and downloading invoices."""
    _require_billing_access(membership)
    org = await db.get(Organization, membership.organization_id)
    if org is None or not org.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="There's no saved payment method yet. It's created with your first payment.",
        )
    try:
        session = await stripe.create_portal_session(
            customer=org.stripe_customer_id, return_url=f"{site_url(request)}/billing"
        )
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc
    return RedirectOut(url=session["url"])


@router.post("/topups", response_model=RedirectOut)
async def buy_topup(
    payload: TopUpRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> RedirectOut:
    """Buy extra sessions or exports. They never expire and are only used
    once the month's included allowance runs out."""
    require_roles(membership, Role.OWNER, Role.FINANCE)
    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    stripe.require_configured()
    base = site_url(request)
    try:
        url = await create_credit_checkout(
            db,
            org=org,
            kind=payload.kind,
            quantity=payload.quantity,
            stripe=stripe,
            user_id=current_user.id,
            customer_email=current_user.email,
            success_url=f"{base}/billing?topup=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing?topup=canceled",
        )
    except StripeError as exc:
        await db.rollback()
        raise _stripe_http_error(exc) from exc
    await db.commit()
    return RedirectOut(url=url)


@router.post("/apply-promotion", response_model=BillingOverviewOut)
async def apply_promotion(
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> BillingOverviewOut:
    """Claims a running promotion for an existing subscription; the discount
    applies to the renewals inside the promotion window."""
    require_roles(membership, Role.OWNER)
    org = await db.get(Organization, membership.organization_id)
    subscription = await current_subscription(db, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    quote = await _claimable_promotion(db, org, subscription)
    if quote is None or subscription is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="There's no promotion to apply right now.")
    try:
        await apply_promotion_to_subscription(
            db, org=org, subscription=subscription, quote=quote, stripe=stripe
        )
    except StripeError as exc:
        raise _stripe_http_error(exc) from exc
    await record_audit_event(
        db,
        action="billing.promotion_applied",
        target_type="billing_subscription",
        target_id=subscription.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={"promotion_id": quote.promotion.id, "cycles": quote.intro_cycles},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return await build_overview(db, org.id)
