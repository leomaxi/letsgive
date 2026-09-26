import base64
from decimal import Decimal
from typing import Any

import httpx
from fastapi import HTTPException, Request, status

from app.core.config import Settings, get_settings


class PayPalClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = (
            "https://api-m.paypal.com"
            if self.settings.paypal_environment.lower() == "live"
            else "https://api-m.sandbox.paypal.com"
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.settings.paypal_client_id and self.settings.paypal_client_secret)

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PayPal is not configured for this deployment.",
            )

    async def _access_token(self) -> str:
        self._require_configured()
        credentials = (
            f"{self.settings.paypal_client_id}:{self.settings.paypal_client_secret}".encode()
        )
        basic_auth = base64.b64encode(credentials).decode()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {basic_auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="PayPal authentication failed.",
            )
        return str(response.json()["access_token"])

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(
                method, f"{self.base_url}{path}", json=json, headers=headers
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"PayPal request failed ({response.status_code}).",
            )
        if not response.content:
            return {}
        return response.json()

    async def create_subscription(
        self,
        *,
        plan_id: str,
        custom_id: str,
        amount: Decimal | None = None,
        currency: str | None = None,
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "custom_id": custom_id,
            "application_context": {
                "brand_name": "Let's Give",
                "locale": "en-US",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        if amount is not None and currency is not None:
            payload["plan"] = {
                "billing_cycles": [
                    {
                        "sequence": 1,
                        "pricing_scheme": {
                            "fixed_price": {
                                "value": f"{amount:.2f}",
                                "currency_code": currency,
                            }
                        },
                    }
                ]
            }
        return await self._request(
            "POST",
            "/v1/billing/subscriptions",
            request_id=request_id,
            json=payload,
        )

    async def cancel_subscription(self, provider_subscription_id: str, reason: str) -> None:
        await self._request(
            "POST",
            f"/v1/billing/subscriptions/{provider_subscription_id}/cancel",
            json={"reason": reason[:128]},
        )

    async def refund_capture(
        self,
        *,
        capture_id: str,
        amount: Decimal,
        currency: str,
        note: str,
        request_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            request_id=request_id,
            json={
                "amount": {"value": f"{amount:.2f}", "currency_code": currency},
                "note_to_payer": note[:255],
            },
        )

    async def verify_webhook(self, request: Request, event: dict[str, Any]) -> bool:
        if not self.settings.paypal_webhook_id:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PayPal webhook verification is not configured.",
            )
        token = await self._access_token()
        headers = request.headers
        payload = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": self.settings.paypal_webhook_id,
            "webhook_event": event,
        }
        if any(payload[key] is None for key in payload if key != "webhook_event"):
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.base_url}/v1/notifications/verify-webhook-signature",
                json=payload,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
        if response.status_code >= 400:
            return False
        return response.json().get("verification_status") == "SUCCESS"


def get_paypal_client() -> PayPalClient:
    return PayPalClient()
