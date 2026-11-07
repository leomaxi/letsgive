import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.domain.stripe_billing import handle_stripe_event
from app.domain.subscriptions import get_billing_settings
from app.integrations.stripe import StripeClient, StripeError, get_stripe_client, verify_webhook_signature

logger = logging.getLogger("letsgive.stripe")

router = APIRouter(prefix="/v1/stripe", tags=["stripe"])

# Events to select when adding the endpoint in the Stripe dashboard.
STRIPE_WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
]


@router.post("/webhook")
async def receive_stripe_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    stripe: StripeClient = Depends(get_stripe_client),
) -> dict[str, str]:
    payload = await request.body()
    settings = await get_billing_settings(db)
    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhook signing secret is not configured.",
        )
    if not verify_webhook_signature(payload, request.headers.get("stripe-signature"), settings.stripe_webhook_secret):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe signature.")
    try:
        event = json.loads(payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON.") from exc

    try:
        await handle_stripe_event(db, event, stripe)
    except StripeError as exc:
        # Non-2xx makes Stripe retry the event later.
        await db.rollback()
        logger.warning("Stripe webhook %s (%s) failed: %s", event.get("id"), event.get("type"), exc.message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    await db.commit()
    return {"status": "processed"}
