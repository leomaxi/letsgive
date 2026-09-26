from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    BillingOverviewOut,
    BillingPaymentOut,
    BillingRefundOut,
    BillingSubscriptionOut,
    StartPayPalSubscriptionRequest,
    StartPayPalSubscriptionResponse,
)
from app.core.config import get_settings
from app.db.models.billing import (
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
    latest_refundable_payment,
    plan_paypal_id,
    discounted_price_from_cents,
    prorated_unused_amount,
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


async def _current_billing_subscription(
    db: AsyncSession, organization_id: str
) -> BillingSubscription | None:
    result = await db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.organization_id == organization_id)
        .order_by(BillingSubscription.created_at.desc())
    )
    return result.scalars().first()


@router.get("", response_model=BillingOverviewOut)
async def get_billing_overview(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> BillingOverviewOut:
    if membership.role not in {Role.OWNER, Role.FINANCE}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Billing access required.")

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
    return BillingOverviewOut(
        subscription=BillingSubscriptionOut.model_validate(subscription) if subscription else None,
        payments=[BillingPaymentOut.model_validate(payment) for payment in payments],
        refunds=[BillingRefundOut.model_validate(refund) for refund in refunds],
        refundable_amount=refundable_amount,
        refundable_payment_id=refundable_payment.id if refundable_payment else None,
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
    provider_plan_id = plan_paypal_id(plan, settings.paypal_plan_ids)
    if provider_plan_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This plan is not mapped to a PayPal plan yet.",
        )

    response = await paypal.create_subscription(
        plan_id=provider_plan_id,
        custom_id=org.id,
        amount=discounted_price_from_cents(plan.monthly_price_cents, org.discount_percent),
        currency="USD",
        return_url=settings.paypal_return_url,
        cancel_url=settings.paypal_cancel_url,
        request_id=f"sub-{org.id}-{plan.id}",
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
        amount=discounted_price_from_cents(plan.monthly_price_cents, org.discount_percent),
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
        after={"plan_id": plan.id, "paypal_subscription_id": provider_subscription_id},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return StartPayPalSubscriptionResponse(
        billing_subscription_id=billing_subscription.id,
        paypal_subscription_id=provider_subscription_id,
        approval_url=_approval_url(response),
    )
