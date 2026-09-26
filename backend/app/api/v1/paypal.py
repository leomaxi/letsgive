from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.domain.subscriptions import apply_paypal_webhook
from app.integrations.paypal import PayPalClient, get_paypal_client

router = APIRouter(prefix="/v1/paypal", tags=["paypal"])


@router.post("/webhook")
async def receive_paypal_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> dict[str, str]:
    event = await request.json()
    if not await paypal.verify_webhook(request, event):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PayPal webhook signature.",
        )
    await apply_paypal_webhook(db, event, paypal)
    await db.commit()
    return {"status": "processed"}
