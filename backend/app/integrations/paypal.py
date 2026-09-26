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

    async def create_product(self, *, name: str, request_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/catalogs/products",
            request_id=request_id,
            json={"name": name, "type": "SERVICE", "category": "SOFTWARE"},
        )

    async def create_plan(
        self,
        *,
        product_id: str,
        name: str,
        interval: str,
        price: Decimal,
        currency: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Creates a billing plan with two cycles: sequence 1 is an intro
        cycle (used for promotional pricing; when no promotion applies it is
        overridden to one cycle at the regular price) and sequence 2 is the
        open-ended regular cycle. PayPal subscription overrides can change
        each cycle's price and count but not its frequency, which is why
        monthly and yearly each need their own plan."""
        interval_unit = "YEAR" if interval == "yearly" else "MONTH"
        money = {"fixed_price": {"value": f"{price:.2f}", "currency_code": currency}}
        frequency = {"interval_unit": interval_unit, "interval_count": 1}
        return await self._request(
            "POST",
            "/v1/billing/plans",
            request_id=request_id,
            json={
                "product_id": product_id,
                "name": name[:127],
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "sequence": 1,
                        "tenure_type": "TRIAL",
                        "total_cycles": 1,
                        "frequency": frequency,
                        "pricing_scheme": money,
                    },
                    {
                        "sequence": 2,
                        "tenure_type": "REGULAR",
                        "total_cycles": 0,
                        "frequency": frequency,
                        "pricing_scheme": money,
                    },
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "payment_failure_threshold": 2,
                },
            },
        )

    @staticmethod
    def billing_cycle_overrides(
        *, intro_amount: Decimal, intro_cycles: int, regular_amount: Decimal, currency: str
    ) -> list[dict[str, Any]]:
        def money(value: Decimal) -> dict[str, Any]:
            return {"fixed_price": {"value": f"{value:.2f}", "currency_code": currency}}

        return [
            {"sequence": 1, "total_cycles": max(intro_cycles, 1), "pricing_scheme": money(intro_amount)},
            {"sequence": 2, "total_cycles": 0, "pricing_scheme": money(regular_amount)},
        ]

    async def create_subscription(
        self,
        *,
        plan_id: str,
        custom_id: str,
        billing_cycles: list[dict[str, Any]],
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/billing/subscriptions",
            request_id=request_id,
            json={
                "plan_id": plan_id,
                "custom_id": custom_id,
                "plan": {"billing_cycles": billing_cycles},
                "application_context": self._application_context(return_url, cancel_url),
            },
        )

    async def revise_subscription(
        self,
        provider_subscription_id: str,
        *,
        plan_id: str,
        billing_cycles: list[dict[str, Any]],
        return_url: str,
        cancel_url: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Pricing changes on a live subscription need the payer's consent;
        PayPal answers with an approve link the owner must follow."""
        return await self._request(
            "POST",
            f"/v1/billing/subscriptions/{provider_subscription_id}/revise",
            request_id=request_id,
            json={
                "plan_id": plan_id,
                "plan": {"billing_cycles": billing_cycles},
                "application_context": self._application_context(return_url, cancel_url),
            },
        )

    @staticmethod
    def _application_context(return_url: str, cancel_url: str) -> dict[str, Any]:
        return {
            "brand_name": "Let's Give",
            "locale": "en-US",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        }

    async def get_subscription(self, provider_subscription_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/billing/subscriptions/{provider_subscription_id}?fields=plan"
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
