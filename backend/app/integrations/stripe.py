"""Minimal Stripe API client (subscriptions, one-off top-ups, refunds,
customer portal) over httpx, in the same shape as the old PayPal client so
tests can swap in a fake via the get_stripe_client dependency.

Credentials come from BillingSettings (entered by a platform admin in the
admin panel), not from environment variables.

API version is pinned so response shapes stay stable regardless of the
Stripe account's default version. Webhook payloads use the *account's*
version, so webhook handling never trusts payload shapes: it re-fetches the
object through this client (see app/domain/stripe_billing.py).
"""
import hashlib
import hmac
import time
from typing import Any

import httpx
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

STRIPE_API_BASE = "https://api.stripe.com"
STRIPE_API_VERSION = "2024-06-20"

NOT_CONFIGURED_DETAIL = (
    "Online payments aren't set up yet. A platform admin needs to add the Stripe "
    "API key under Admin → Pricing → Stripe payments."
)


class StripeError(Exception):
    def __init__(self, message: str, status_code: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _flatten(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Encodes nested dicts/lists the way Stripe's form API expects:
    line_items[0][price_data][currency]=usd."""
    items: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if value is None:
                continue
            items.extend(_flatten(value, f"{prefix}[{key}]" if prefix else str(key)))
    elif isinstance(data, (list, tuple)):
        for index, value in enumerate(data):
            items.extend(_flatten(value, f"{prefix}[{index}]"))
    elif isinstance(data, bool):
        items.append((prefix, "true" if data else "false"))
    else:
        items.append((prefix, str(data)))
    return items


def key_mode(secret_key: str | None) -> str | None:
    if not secret_key:
        return None
    if "_test_" in secret_key:
        return "test"
    if "_live_" in secret_key:
        return "live"
    return "unknown"


class StripeClient:
    def __init__(self, secret_key: str | None) -> None:
        self.secret_key = secret_key or None

    @property
    def is_configured(self) -> bool:
        return bool(self.secret_key)

    def require_configured(self) -> None:
        if not self.is_configured:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED_DETAIL)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        params: list[tuple[str, str]] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.require_configured()
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Stripe-Version": STRIPE_API_VERSION,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.request(
                    method,
                    f"{STRIPE_API_BASE}{path}",
                    data=_flatten(data) if data else None,
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise StripeError("Couldn't reach Stripe. Please try again in a moment.") from exc
        body: dict[str, Any] = response.json() if response.content else {}
        if response.status_code >= 400:
            error = body.get("error") or {}
            message = error.get("message") or f"Stripe request failed ({response.status_code})."
            # Card/payment problems are the payer's to fix, so pass Stripe's
            # own wording through; everything else is a server-side problem.
            code = 402 if error.get("type") == "card_error" else 502
            if response.status_code == 401:
                message = "Stripe rejected the API key. Check the key in the admin panel."
            raise StripeError(message, status_code=code, code=error.get("code"))
        return body

    # -- account -------------------------------------------------------
    async def retrieve_account(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/account")

    # -- customers / products / coupons --------------------------------
    async def create_customer(self, *, name: str, email: str | None, metadata: dict[str, str], idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/customers",
            data={"name": name, "email": email, "metadata": metadata},
            idempotency_key=idempotency_key,
        )

    async def create_product(self, *, name: str, metadata: dict[str, str], idempotency_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/products", data={"name": name, "metadata": metadata}, idempotency_key=idempotency_key
        )

    async def create_coupon(
        self, *, name: str, percent_off: int, duration_in_months: int, idempotency_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/coupons",
            data={
                "name": name[:40],
                "percent_off": percent_off,
                "duration": "repeating",
                "duration_in_months": max(duration_in_months, 1),
            },
            idempotency_key=idempotency_key,
        )

    # -- checkout -------------------------------------------------------
    async def create_checkout_session(self, params: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        return await self._request("POST", "/v1/checkout/sessions", data=params, idempotency_key=idempotency_key)

    async def retrieve_checkout_session(self, checkout_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/checkout/sessions/{checkout_id}")

    # -- subscriptions ---------------------------------------------------
    async def retrieve_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/subscriptions/{subscription_id}")

    async def update_subscription(
        self, subscription_id: str, params: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST", f"/v1/subscriptions/{subscription_id}", data=params, idempotency_key=idempotency_key
        )

    async def cancel_subscription_now(self, subscription_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/subscriptions/{subscription_id}")

    # -- invoices / refunds / portal --------------------------------------
    async def retrieve_invoice(self, invoice_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/invoices/{invoice_id}")

    async def retrieve_payment_intent(self, payment_intent_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/v1/payment_intents/{payment_intent_id}", params=[("expand[]", "latest_charge")]
        )

    async def create_refund(
        self, *, payment_intent: str, amount_cents: int, metadata: dict[str, str], idempotency_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/refunds",
            data={"payment_intent": payment_intent, "amount": amount_cents, "metadata": metadata},
            idempotency_key=idempotency_key,
        )

    async def create_portal_session(self, *, customer: str, return_url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/billing_portal/sessions", data={"customer": customer, "return_url": return_url}
        )


def verify_webhook_signature(payload: bytes, signature_header: str | None, secret: str, tolerance: int = 300) -> bool:
    """Verifies a Stripe-Signature header (t=...,v1=...) per Stripe's
    documented scheme: HMAC-SHA256 of "{t}.{payload}" with the endpoint's
    signing secret, rejecting stale timestamps to block replays."""
    if not signature_header or not secret:
        return False
    timestamp: str | None = None
    signatures: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        return False
    try:
        if abs(time.time() - int(timestamp)) > tolerance:
            return False
    except ValueError:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, signature) for signature in signatures)


async def get_stripe_client(db: AsyncSession = Depends(get_db)) -> StripeClient:
    from app.domain.subscriptions import get_billing_settings

    settings = await get_billing_settings(db)
    return StripeClient(settings.stripe_secret_key)
