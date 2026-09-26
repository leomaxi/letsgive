from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    BillingOverviewOut,
    BillingPaymentOut,
    BillingPricingOut,
    BillingRefundOut,
    BillingSubscriptionOut,
    PlanPriceQuoteOut,
    PromotionSummaryOut,
    RevisePayPalSubscriptionResponse,
    StartPayPalSubscriptionRequest,
    StartPayPalSubscriptionResponse,
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
from app.domain.rbac import require_roles
from app.domain.subscriptions import (
    SubscriptionQuote,
    ensure_paypal_plan_id,
    get_billing_settings,
    latest_refundable_payment,
    prorated_unused_amount,
    quote_subscription,
)
from app.integrations.paypal import PayPalClient, get_paypal_client

router = APIRouter(prefix="/v1/organizations/{organization_id}/billing", tags=["billing"])


def _approval_url(response: dict) -> str:
    for link in response.get("links", []):
        if link.get("rel") == "approve" and link.get("href"):
            return str(link["href"])
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="PayPal did not return a subscription approval link.",
    )


def _require_billing_access(membership: Membership) -> None:
    if membership.role not in {Role.OWNER, Role.FINANCE}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Billing access required.")


async def _current_billing_subscription(
    db: AsyncSession, organization_id: str
) -> BillingSubscription | None:
    result = await db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.organization_id == organization_id)
        .order_by(BillingSubscription.created_at.desc())
    )
    return result.scalars().first()


async def _payments_made(db: AsyncSession, subscription: BillingSubscription) -> int:
    result = await db.execute(
        select(func.count(BillingPayment.id)).where(
            BillingPayment.billing_subscription_id == subscription.id
        )
    )
    return result.scalar_one()


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


async def _pricing_update_for(
    db: AsyncSession, org: Organization, subscription: BillingSubscription
) -> SubscriptionQuote | None:
    """Quote for an active subscription's upcoming charges, returned only
    when it differs from what PayPal is currently set up to charge (admin
    price change, a promotion for existing subscribers, or a new annual
    savings percentage)."""
    if subscription.status != BillingSubscriptionStatus.ACTIVE:
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
    regular = subscription.regular_amount if subscription.regular_amount is not None else subscription.amount
    remaining_promo = max(subscription.promo_cycles - await _payments_made(db, subscription), 0)
    current_next = subscription.amount if remaining_promo > 0 else regular
    if quote.regular_amount == regular and quote.intro_amount == current_next:
        return None
    return quote


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

    subscription = await _current_billing_subscription(db, membership.organization_id)
    payments_result = await db.execute(
        select(BillingPayment)
        .where(BillingPayment.organization_id == membership.organization_id)
        .order_by(BillingPayment.created_at.desc())
    )
    payments = list(payments_result.scalars().all())
    refunds_result = await db.execute(
        select(BillingRefund)
        .where(BillingRefund.organization_id == membership.organization_id)
        .order_by(BillingRefund.created_at.desc())
    )
    refunds = list(refunds_result.scalars().all())
    refundable_payment = await latest_refundable_payment(db, membership.organization_id)
    refundable_amount = (
        prorated_unused_amount(refundable_payment) if refundable_payment is not None else Decimal("0.00")
    )
    pricing_update: PlanPriceQuoteOut | None = None
    org = await db.get(Organization, membership.organization_id)
    if subscription is not None and org is not None:
        update = await _pricing_update_for(db, org, subscription)
        if update is not None:
            pricing_update = _quote_out(subscription.plan_id, update)
    return BillingOverviewOut(
        subscription=BillingSubscriptionOut.model_validate(subscription) if subscription else None,
        payments=[BillingPaymentOut.model_validate(payment) for payment in payments],
        refunds=[BillingRefundOut.model_validate(refund) for refund in refunds],
        refundable_amount=refundable_amount,
        refundable_payment_id=refundable_payment.id if refundable_payment else None,
        pricing_update=pricing_update,
    )


@router.post("/paypal-subscription", response_model=StartPayPalSubscriptionResponse)
async def start_paypal_subscription(
    payload: StartPayPalSubscriptionRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> StartPayPalSubscriptionResponse:
    require_roles(membership, Role.OWNER)

    org = await db.get(Organization, membership.organization_id)
    plan = await db.get(Plan, payload.plan_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")
    if plan.monthly_price_cents <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This plan does not require a PayPal subscription.",
        )

    settings = get_settings()
    quote = await quote_subscription(
        db,
        plan=plan,
        org_discount_percent=org.discount_percent,
        interval=payload.interval,
    )
    provider_plan_id = await ensure_paypal_plan_id(
        db, plan=plan, interval=payload.interval, paypal=paypal
    )
    response = await paypal.create_subscription(
        plan_id=provider_plan_id,
        custom_id=org.id,
        billing_cycles=paypal.billing_cycle_overrides(
            intro_amount=quote.intro_amount,
            intro_cycles=quote.intro_cycles,
            regular_amount=quote.regular_amount,
            currency="USD",
        ),
        return_url=settings.paypal_return_url,
        cancel_url=settings.paypal_cancel_url,
        request_id=f"sub-{org.id}-{plan.id}-{payload.interval.value}-{utcnow():%Y%m%d%H%M}",
    )
    provider_subscription_id = response.get("id")
    if not provider_subscription_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="PayPal did not return a subscription id.",
        )

    billing_subscription = BillingSubscription(
        organization_id=org.id,
        plan_id=plan.id,
        provider_subscription_id=provider_subscription_id,
        status=BillingSubscriptionStatus.APPROVAL_PENDING,
        currency="USD",
        amount=quote.intro_amount,
        regular_amount=quote.regular_amount,
        promo_cycles=quote.intro_cycles,
        interval=payload.interval,
        annual_savings_percent=quote.annual_savings_percent,
        promotion_id=quote.promotion.id if quote.promotion else None,
    )
    db.add(billing_subscription)
    await db.flush()

    await record_audit_event(
        db,
        action="paypal.subscription_approval_started",
        target_type="billing_subscription",
        target_id=billing_subscription.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={
            "plan_id": plan.id,
            "paypal_subscription_id": provider_subscription_id,
            "interval": payload.interval.value,
            "intro_amount": str(quote.intro_amount),
            "intro_cycles": quote.intro_cycles,
            "regular_amount": str(quote.regular_amount),
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return StartPayPalSubscriptionResponse(
        billing_subscription_id=billing_subscription.id,
        paypal_subscription_id=provider_subscription_id,
        approval_url=_approval_url(response),
    )


@router.post("/paypal-subscription/revise", response_model=RevisePayPalSubscriptionResponse)
async def revise_paypal_subscription(
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> RevisePayPalSubscriptionResponse:
    """Moves an active subscription onto current pricing. PayPal requires
    the payer to approve pricing changes, so this returns an approval link;
    stored amounts change only once confirm reads them back from PayPal."""
    require_roles(membership, Role.OWNER)
    org = await db.get(Organization, membership.organization_id)
    subscription = await _current_billing_subscription(db, membership.organization_id)
    if org is None or subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")
    quote = await _pricing_update_for(db, org, subscription)
    if quote is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your subscription already uses current pricing.",
        )
    plan = await db.get(Plan, subscription.plan_id)
    provider_plan_id = await ensure_paypal_plan_id(
        db, plan=plan, interval=subscription.interval, paypal=paypal
    )
    settings = get_settings()
    response = await paypal.revise_subscription(
        subscription.provider_subscription_id,
        plan_id=provider_plan_id,
        billing_cycles=paypal.billing_cycle_overrides(
            intro_amount=quote.intro_amount,
            intro_cycles=quote.intro_cycles,
            regular_amount=quote.regular_amount,
            currency=subscription.currency,
        ),
        return_url=settings.paypal_return_url.replace("paypal=approved", "paypal=revised"),
        cancel_url=settings.paypal_cancel_url,
        request_id=f"revise-{subscription.id}-{utcnow():%Y%m%d%H%M}",
    )
    await record_audit_event(
        db,
        action="paypal.subscription_revision_started",
        target_type="billing_subscription",
        target_id=subscription.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        before={
            "amount": str(subscription.amount),
            "regular_amount": str(subscription.regular_amount),
        },
        after={
            "intro_amount": str(quote.intro_amount),
            "intro_cycles": quote.intro_cycles,
            "regular_amount": str(quote.regular_amount),
            "promotion_id": quote.promotion.id if quote.promotion else None,
        },
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return RevisePayPalSubscriptionResponse(approval_url=_approval_url(response))


def _cycle_price(cycle: dict) -> Decimal | None:
    value = ((cycle.get("pricing_scheme") or {}).get("fixed_price") or {}).get("value")
    return Decimal(str(value)).quantize(Decimal("0.01")) if value is not None else None


@router.post("/paypal-subscription/revise/confirm", response_model=BillingOverviewOut)
async def confirm_paypal_subscription_revision(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> BillingOverviewOut:
    """Called when the owner returns from PayPal after approving a revision.
    Reads the subscription's pricing back from PayPal rather than trusting
    the redirect, so an abandoned approval leaves the stored amounts alone."""
    require_roles(membership, Role.OWNER)
    org = await db.get(Organization, membership.organization_id)
    subscription = await _current_billing_subscription(db, membership.organization_id)
    if org is None or subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No subscription found.")

    details = await paypal.get_subscription(subscription.provider_subscription_id)
    cycles = {
        int(cycle.get("sequence", 0)): cycle
        for cycle in ((details.get("plan") or {}).get("billing_cycles") or [])
    }
    intro_price = _cycle_price(cycles.get(1) or {})
    regular_price = _cycle_price(cycles.get(2) or {})
    if intro_price is not None and regular_price is not None:
        quote = await _pricing_update_for(db, org, subscription)
        before = {"amount": str(subscription.amount), "regular_amount": str(subscription.regular_amount)}
        promo_cycles = int(cycles[1].get("total_cycles") or 0) if intro_price < regular_price else 0
        subscription.amount = intro_price
        subscription.regular_amount = regular_price
        # promo_cycles is counted from the subscription's first payment,
        # while revised promo cycles start at the next one.
        subscription.promo_cycles = (
            await _payments_made(db, subscription) + promo_cycles if promo_cycles else 0
        )
        subscription.promotion_id = (
            quote.promotion.id if quote is not None and quote.promotion and promo_cycles else None
        )
        if quote is not None:
            subscription.annual_savings_percent = quote.annual_savings_percent
        if before != {"amount": str(subscription.amount), "regular_amount": str(subscription.regular_amount)}:
            await record_audit_event(
                db,
                action="paypal.subscription_revised",
                target_type="billing_subscription",
                target_id=subscription.id,
                organization_id=org.id,
                actor_user_id=membership.user_id,
                before=before,
                after={
                    "amount": str(subscription.amount),
                    "regular_amount": str(subscription.regular_amount),
                },
            )
        await db.commit()
    return await get_billing_overview(membership=membership, db=db)
