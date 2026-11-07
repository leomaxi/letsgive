import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import (
    BillingInterval,
    BillingPayment,
    BillingPaymentStatus,
    BillingPromotion,
    BillingSubscription,
    BillingSubscriptionStatus,
    CreditPurchase,
)
from app.db.models.export_usage import ExportUsage
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.domain.subscriptions import get_billing_settings
from app.integrations.stripe import StripeError, get_stripe_client
from app.main import app
from tests.helpers import (
    add_accepted_contribution,
    add_active_member,
    create_fake_connection,
    create_org,
    create_session,
    enable_mfa,
    make_platform_admin,
    register_and_login,
)

ORIGIN = {"Origin": "https://letsgive.ca"}
WEBHOOK_SECRET = "whsec_test_secret"


class FakeStripe:
    """In-memory stand-in for StripeClient that keeps enough state for the
    flows under test (customers, checkouts, subscriptions, invoices)."""

    secret_key = "sk_test_fake"
    is_configured = True

    def __init__(self) -> None:
        self.counter = 0
        self.customers: list[dict] = []
        self.products: list[dict] = []
        self.coupons: dict[str, dict] = {}
        self.checkouts: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.updates: list[tuple[str, dict]] = []
        self.refunds: list[dict] = []
        self.fail_updates = False

    def require_configured(self) -> None:
        return None

    def _next(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}_{self.counter}"

    async def retrieve_account(self) -> dict:
        return {"business_profile": {"name": "Let's Give"}, "charges_enabled": True}

    async def create_customer(self, **kwargs) -> dict:
        customer = {"id": self._next("cus"), **kwargs}
        self.customers.append(customer)
        return customer

    async def create_product(self, **kwargs) -> dict:
        product = {"id": self._next("prod"), **kwargs}
        self.products.append(product)
        return product

    async def create_coupon(self, **kwargs) -> dict:
        coupon = {"id": self._next("co"), **kwargs}
        self.coupons[coupon["id"]] = coupon
        return coupon

    async def create_checkout_session(self, params: dict, *, idempotency_key: str) -> dict:
        checkout_id = self._next("cs")
        checkout = {
            "id": checkout_id,
            "url": f"https://checkout.stripe.test/{checkout_id}",
            "status": "open",
            "payment_status": "unpaid",
            **params,
        }
        self.checkouts[checkout_id] = checkout
        return checkout

    async def retrieve_checkout_session(self, checkout_id: str) -> dict:
        if checkout_id not in self.checkouts:
            raise StripeError("No such checkout session.", status_code=404)
        return self.checkouts[checkout_id]

    async def retrieve_subscription(self, subscription_id: str) -> dict:
        return self.subscriptions[subscription_id]

    async def update_subscription(self, subscription_id: str, params: dict, *, idempotency_key=None) -> dict:
        if self.fail_updates:
            raise StripeError("Your card was declined.", status_code=402)
        self.updates.append((subscription_id, params))
        sub = self.subscriptions[subscription_id]
        if "items" in params:
            price_data = params["items"][0]["price_data"]
            sub["items"]["data"][0]["price"] = {
                "unit_amount": price_data["unit_amount"],
                "currency": price_data["currency"],
                "recurring": price_data["recurring"],
            }
        if "metadata" in params:
            sub["metadata"] = {**sub.get("metadata", {}), **params["metadata"]}
        if "cancel_at_period_end" in params:
            sub["cancel_at_period_end"] = params["cancel_at_period_end"]
        if "discounts" in params:
            coupon = self.coupons[params["discounts"][0]["coupon"]]
            sub["discount"] = {"coupon": {"percent_off": coupon["percent_off"]}}
        return sub

    async def retrieve_invoice(self, invoice_id: str) -> dict:
        return self.invoices[invoice_id]

    async def retrieve_payment_intent(self, payment_intent_id: str) -> dict:
        return {"id": payment_intent_id, "latest_charge": {"receipt_url": f"https://pay.stripe.test/{payment_intent_id}"}}

    async def create_refund(self, **kwargs) -> dict:
        self.refunds.append(kwargs)
        return {"id": "re_1", "status": "succeeded"}

    async def create_portal_session(self, *, customer: str, return_url: str) -> dict:
        return {"url": f"https://billing.stripe.test/{customer}"}

    # helpers for tests ---------------------------------------------------
    def add_subscription(
        self,
        *,
        org_id: str,
        plan_id: str,
        unit_amount: int,
        interval: str = "month",
        status: str = "active",
        customer: str = "cus_existing",
    ) -> dict:
        now = int(time.time())
        sub_id = self._next("sub")
        sub = {
            "id": sub_id,
            "customer": customer,
            "status": status,
            "cancel_at_period_end": False,
            "current_period_start": now,
            "current_period_end": now + (365 if interval == "year" else 30) * 86400,
            "metadata": {"organization_id": org_id, "plan_id": plan_id, "interval": "yearly" if interval == "year" else "monthly"},
            "items": {
                "data": [
                    {
                        "id": self._next("si"),
                        "price": {"unit_amount": unit_amount, "currency": "usd", "recurring": {"interval": interval}},
                    }
                ]
            },
        }
        self.subscriptions[sub_id] = sub
        return sub

    def add_invoice(self, *, subscription_id: str, amount_paid: int, reason: str = "subscription_cycle") -> dict:
        invoice_id = self._next("in")
        now = int(time.time())
        invoice = {
            "id": invoice_id,
            "status": "paid",
            "amount_paid": amount_paid,
            "currency": "usd",
            "subscription": subscription_id,
            "payment_intent": self._next("pi"),
            "hosted_invoice_url": f"https://invoice.stripe.test/{invoice_id}",
            "billing_reason": reason,
            "lines": {"data": [{"period": {"start": now, "end": now + 30 * 86400}}]},
        }
        self.invoices[invoice_id] = invoice
        return invoice


def use_fake_stripe() -> FakeStripe:
    fake = FakeStripe()
    app.dependency_overrides[get_stripe_client] = lambda: fake
    return fake


def stop_fake_stripe() -> None:
    app.dependency_overrides.pop(get_stripe_client, None)


async def _owner_org(client: AsyncClient, prefix: str, plan_key: str | None = None, db_session=None):
    token = await register_and_login(client, f"owner-{prefix}@example.org")
    await enable_mfa(client, token)
    org = await create_org(client, token, f"{prefix} Org")
    if plan_key and db_session is not None:
        plan = (await db_session.execute(select(Plan).where(Plan.key == plan_key))).scalar_one()
        org_row = await db_session.get(Organization, org["id"])
        org_row.plan_id = plan.id
        await db_session.commit()
    return token, org, {"Authorization": f"Bearer {token}", **ORIGIN}


async def _plan(db_session: AsyncSession, key: str) -> Plan:
    return (await db_session.execute(select(Plan).where(Plan.key == key))).scalar_one()


async def _link_subscription(
    db_session: AsyncSession, fake: FakeStripe, org_id: str, plan: Plan, unit_amount: int, interval: str = "month"
) -> BillingSubscription:
    """An org already paying through Stripe."""
    remote = fake.add_subscription(org_id=org_id, plan_id=plan.id, unit_amount=unit_amount, interval=interval)
    org_row = await db_session.get(Organization, org_id)
    org_row.plan_id = plan.id
    org_row.stripe_customer_id = remote["customer"]
    subscription = BillingSubscription(
        organization_id=org_id,
        plan_id=plan.id,
        provider="stripe",
        provider_subscription_id=remote["id"],
        status=BillingSubscriptionStatus.ACTIVE,
        currency="USD",
        amount=Decimal(unit_amount) / 100,
        regular_amount=Decimal(unit_amount) / 100,
        interval=BillingInterval.YEARLY if interval == "year" else BillingInterval.MONTHLY,
        current_period_start=datetime.fromtimestamp(remote["current_period_start"], timezone.utc),
        current_period_end=datetime.fromtimestamp(remote["current_period_end"], timezone.utc),
    )
    db_session.add(subscription)
    await db_session.commit()
    return subscription


def _signed(event: dict) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps(event).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": f"t={timestamp},v1={signature}", "Content-Type": "application/json"}


async def _set_webhook_secret(db_session: AsyncSession) -> None:
    settings = await get_billing_settings(db_session)
    settings.stripe_webhook_secret = WEBHOOK_SECRET
    await db_session.commit()


# --- subscribing --------------------------------------------------------------
async def test_checkout_needs_stripe_configured(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "nostripe")
    growth = await _plan(db_session, "growth")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/billing/checkout", json={"plan_id": growth.id}, headers=headers
    )
    assert resp.status_code == 503, resp.text
    assert "Stripe" in resp.json()["detail"]


async def test_new_subscription_goes_to_stripe_checkout(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "newsub")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout",
            json={"plan_id": growth.id, "interval": "monthly"},
            headers=headers,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "redirect"
    assert body["url"].startswith("https://checkout.stripe.test/")

    checkout = next(iter(fake.checkouts.values()))
    assert checkout["mode"] == "subscription"
    price = checkout["line_items"][0]["price_data"]
    assert price["unit_amount"] == 700
    assert price["recurring"] == {"interval": "month"}  # Stripe renews it automatically
    assert checkout["subscription_data"]["metadata"]["plan_id"] == growth.id
    assert checkout["success_url"] == "https://letsgive.ca/billing?stripe=success&session_id={CHECKOUT_SESSION_ID}"
    assert checkout["cancel_url"] == "https://letsgive.ca/billing?stripe=canceled"
    # The org now has a Stripe customer, reused next time.
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.stripe_customer_id == fake.customers[0]["id"]
    # Nothing changes until payment completes.
    assert org_row.plan_id != growth.id


async def test_yearly_checkout_applies_savings_and_time_boxed_promotion(
    client: AsyncClient, db_session: AsyncSession
):
    _, org, headers = await _owner_org(client, "yearly")
    growth = await _plan(db_session, "growth")
    settings = await get_billing_settings(db_session)
    settings.annual_savings_percent = 10
    db_session.add(
        BillingPromotion(
            name="Launch",
            percent_off=20,
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            ends_at=datetime.now(timezone.utc) + timedelta(days=30),
            plan_id=growth.id,
            applies_to_existing=True,
            applies_to_new=True,
            is_active=True,
        )
    )
    await db_session.commit()
    fake = use_fake_stripe()
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout",
            json={"plan_id": growth.id, "interval": "yearly"},
            headers=headers,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    checkout = next(iter(fake.checkouts.values()))
    # $7 x 12 = $84, less 10% yearly saving = $75.60 a year, renewing yearly.
    assert checkout["line_items"][0]["price_data"]["unit_amount"] == 7560
    assert checkout["line_items"][0]["price_data"]["recurring"] == {"interval": "year"}
    # The 20% promotion covers exactly the one yearly payment inside its window.
    coupon = fake.coupons[checkout["discounts"][0]["coupon"]]
    assert coupon["percent_off"] == 20
    assert coupon["duration_in_months"] == 12


async def test_returning_from_checkout_activates_plan_and_records_payment(
    client: AsyncClient, db_session: AsyncSession
):
    _, org, headers = await _owner_org(client, "confirm")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    try:
        await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout", json={"plan_id": growth.id}, headers=headers
        )
        checkout = next(iter(fake.checkouts.values()))
        remote = fake.add_subscription(org_id=org["id"], plan_id=growth.id, unit_amount=700)
        invoice = fake.add_invoice(subscription_id=remote["id"], amount_paid=700, reason="subscription_create")
        checkout.update(status="complete", payment_status="paid", subscription=remote["id"], invoice=invoice["id"])

        for _ in range(2):  # the redirect and a page reload: still one payment
            resp = await client.post(
                f"/v1/organizations/{org['id']}/billing/checkout/confirm",
                json={"session_id": checkout["id"]},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
    finally:
        stop_fake_stripe()

    body = resp.json()
    assert body["subscription"]["status"] == "active"
    assert body["subscription"]["interval"] == "monthly"
    assert body["subscription"]["current_period_end"] is not None
    assert len(body["payments"]) == 1
    assert Decimal(body["payments"][0]["amount"]) == Decimal("7.00")
    assert body["payments"][0]["receipt_url"].startswith("https://invoice.stripe.test/")
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.plan_id == growth.id
    assert org_row.subscription_status.value == "active"


async def test_confirm_rejects_another_orgs_checkout(client: AsyncClient, db_session: AsyncSession):
    _, org_a, headers_a = await _owner_org(client, "confirma")
    _, org_b, headers_b = await _owner_org(client, "confirmb")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    try:
        await client.post(
            f"/v1/organizations/{org_a['id']}/billing/checkout", json={"plan_id": growth.id}, headers=headers_a
        )
        checkout = next(iter(fake.checkouts.values()))
        resp = await client.post(
            f"/v1/organizations/{org_b['id']}/billing/checkout/confirm",
            json={"session_id": checkout["id"]},
            headers=headers_b,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 404


async def test_existing_subscriber_switches_plan_in_place(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "switchplace")
    growth = await _plan(db_session, "growth")
    premium = await _plan(db_session, "premium")
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout",
            json={"plan_id": premium.id, "interval": "yearly"},
            headers=headers,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "updated"
    assert fake.checkouts == {}  # no second subscription, so no double billing
    sub_id, params = fake.updates[0]
    assert sub_id == subscription.provider_subscription_id
    assert params["items"][0]["id"].startswith("si_")
    assert params["items"][0]["price_data"]["unit_amount"] == 14352  # $13 x 12 less 8%
    assert params["proration_behavior"] == "always_invoice"
    assert params["payment_behavior"] == "error_if_incomplete"
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.plan_id == premium.id
    assert resp.json()["overview"]["subscription"]["interval"] == "yearly"


async def test_declined_card_on_plan_change_leaves_plan_alone(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "declined")
    growth = await _plan(db_session, "growth")
    premium = await _plan(db_session, "premium")
    fake = use_fake_stripe()
    await _link_subscription(db_session, fake, org["id"], growth, 700)
    fake.fail_updates = True
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout", json={"plan_id": premium.id}, headers=headers
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 402
    assert "declined" in resp.json()["detail"]
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.plan_id == growth.id


async def test_cancel_keeps_plan_until_period_end_and_can_resume(
    client: AsyncClient, db_session: AsyncSession
):
    _, org, headers = await _owner_org(client, "cancelresume")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    try:
        resp = await client.post(f"/v1/organizations/{org['id']}/billing/cancel", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription"]["cancel_at_period_end"] is True
        org_row = await db_session.get(Organization, org["id"])
        await db_session.refresh(org_row)
        assert org_row.plan_id == growth.id  # still on Growth until the period ends
        assert org_row.subscription_status.value == "active"

        resp = await client.post(f"/v1/organizations/{org['id']}/billing/resume", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription"]["cancel_at_period_end"] is False
    finally:
        stop_fake_stripe()
    assert fake.subscriptions[subscription.provider_subscription_id]["cancel_at_period_end"] is False


async def test_old_cancel_endpoint_also_stops_auto_renew(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "oldcancel")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    await _link_subscription(db_session, fake, org["id"], growth, 700)
    try:
        resp = await client.post(f"/v1/organizations/{org['id']}/subscription/cancel", headers=headers)
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_status"] == "active"
    assert resp.json()["plan_id"] == growth.id
    assert fake.updates[-1][1] == {"cancel_at_period_end": True}


async def test_switch_plan_endpoint_cannot_grant_a_paid_plan(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "freeupgrade")
    premium = await _plan(db_session, "premium")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan", json={"plan_id": premium.id}, headers=headers
    )
    assert resp.status_code == 402
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.plan_id != premium.id


# --- webhooks -------------------------------------------------------------------
async def test_webhook_rejects_bad_signature(client: AsyncClient, db_session: AsyncSession):
    await _set_webhook_secret(db_session)
    payload, headers = _signed({"type": "invoice.paid", "data": {"object": {"id": "in_x"}}})
    headers["Stripe-Signature"] = headers["Stripe-Signature"][:-4] + "0000"
    fake = use_fake_stripe()
    try:
        resp = await client.post("/v1/stripe/webhook", content=payload, headers=headers)
    finally:
        stop_fake_stripe()
    assert resp.status_code == 400


async def test_renewal_webhook_records_payment_once(client: AsyncClient, db_session: AsyncSession):
    _, org, _ = await _owner_org(client, "renewal")
    growth = await _plan(db_session, "growth")
    await _set_webhook_secret(db_session)
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    invoice = fake.add_invoice(subscription_id=subscription.provider_subscription_id, amount_paid=700)
    event = {"id": "evt_1", "type": "invoice.paid", "data": {"object": {"id": invoice["id"]}}}
    try:
        for _ in range(2):  # Stripe may deliver an event more than once
            payload, headers = _signed(event)
            resp = await client.post("/v1/stripe/webhook", content=payload, headers=headers)
            assert resp.status_code == 200, resp.text
    finally:
        stop_fake_stripe()
    payments = (
        await db_session.execute(select(BillingPayment).where(BillingPayment.organization_id == org["id"]))
    ).scalars().all()
    assert len(payments) == 1
    assert payments[0].amount == Decimal("7.00")
    assert payments[0].provider_capture_id == invoice["payment_intent"]


async def test_failed_payment_marks_past_due_and_notifies(
    client: AsyncClient, db_session: AsyncSession, notifier
):
    _, org, _ = await _owner_org(client, "pastdue")
    growth = await _plan(db_session, "growth")
    await _set_webhook_secret(db_session)
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    fake.subscriptions[subscription.provider_subscription_id]["status"] = "past_due"
    invoice = fake.add_invoice(subscription_id=subscription.provider_subscription_id, amount_paid=0)
    invoice["status"] = "open"
    try:
        payload, headers = _signed({"type": "invoice.payment_failed", "data": {"object": {"id": invoice["id"]}}})
        resp = await client.post("/v1/stripe/webhook", content=payload, headers=headers)
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    assert org_row.subscription_status.value == "past_due"
    assert any("payment failed" in entry.get("subject", "").lower() for entry in notifier.sent)


async def test_ended_subscription_moves_org_to_free_plan(client: AsyncClient, db_session: AsyncSession):
    _, org, _ = await _owner_org(client, "ended")
    growth = await _plan(db_session, "growth")
    starter = await _plan(db_session, "starter")
    await _set_webhook_secret(db_session)
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    fake.subscriptions[subscription.provider_subscription_id]["status"] = "canceled"
    try:
        payload, headers = _signed(
            {
                "type": "customer.subscription.deleted",
                "data": {"object": {"id": subscription.provider_subscription_id}},
            }
        )
        resp = await client.post("/v1/stripe/webhook", content=payload, headers=headers)
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    org_row = await db_session.get(Organization, org["id"])
    await db_session.refresh(org_row)
    # Moved to the free plan -- never locked out.
    assert org_row.plan_id == starter.id
    assert org_row.subscription_status.value == "active"


# --- top-ups & credits -------------------------------------------------------------
async def test_topup_purchase_grants_credits_exactly_once(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "topup")
    fake = use_fake_stripe()
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/topups",
            json={"kind": "sessions", "quantity": 5},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        checkout = next(iter(fake.checkouts.values()))
        assert checkout["mode"] == "payment"
        assert checkout["line_items"][0]["price_data"]["unit_amount"] == 300
        assert checkout["line_items"][0]["quantity"] == 5
        assert checkout["success_url"].startswith("https://letsgive.ca/billing?topup=success")

        # Not paid yet: no credits.
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/checkout/confirm",
            json={"session_id": checkout["id"]},
            headers=headers,
        )
        assert resp.json()["usage"]["session_credits"] == 0

        checkout.update(status="complete", payment_status="paid", payment_intent="pi_topup", amount_total=1500, currency="usd")
        for _ in range(2):
            resp = await client.post(
                f"/v1/organizations/{org['id']}/billing/checkout/confirm",
                json={"session_id": checkout["id"]},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
    finally:
        stop_fake_stripe()
    body = resp.json()
    assert body["usage"]["session_credits"] == 5
    topup_payment = next(p for p in body["payments"] if p["kind"] == "session_credits")
    assert Decimal(topup_payment["amount"]) == Decimal("15.00")
    assert topup_payment["receipt_url"] == "https://pay.stripe.test/pi_topup"
    purchase = (
        await db_session.execute(select(CreditPurchase).where(CreditPurchase.organization_id == org["id"]))
    ).scalar_one()
    assert purchase.status == "completed"


async def test_purchased_sessions_are_used_only_after_the_monthly_allowance(
    client: AsyncClient, db_session: AsyncSession
):
    token, org, _ = await _owner_org(client, "credituse")
    media_token = await add_active_member(client, db_session, token, org["id"], "media-credituse@example.org", "media")
    org_row = await db_session.get(Organization, org["id"])
    org_row.session_credits = 1
    await db_session.commit()

    # Starter includes 2 a month: these don't touch the purchased credit.
    for _ in range(2):
        await create_session(client, media_token, org["id"])
    await db_session.refresh(org_row)
    assert org_row.session_credits == 1

    await create_session(client, media_token, org["id"])  # uses the credit
    await db_session.refresh(org_row)
    assert org_row.session_credits == 0

    resp = await client.post(
        "/v1/sessions",
        json={"organization_id": org["id"], "contribution_method": "e-transfer", "duration_seconds": 600},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 402
    assert "Buy extra sessions" in resp.json()["detail"]


async def test_overview_reports_usage(client: AsyncClient, db_session: AsyncSession):
    token, org, headers = await _owner_org(client, "usage")
    media_token = await add_active_member(client, db_session, token, org["id"], "media-usage@example.org", "media")
    await create_session(client, media_token, org["id"])
    org_row = await db_session.get(Organization, org["id"])
    org_row.export_credits = 4
    await db_session.commit()
    resp = await client.get(f"/v1/organizations/{org['id']}/billing", headers=headers)
    assert resp.status_code == 200, resp.text
    usage = resp.json()["usage"]
    assert usage["sessions_used"] == 1
    assert usage["sessions_included"] == 2
    assert usage["exports_included"] == 0
    assert usage["export_credits"] == 4
    assert resp.json()["payments_enabled"] is False
    assert resp.json()["session_topup_price_cents"] == 300


# --- exports -------------------------------------------------------------------------
async def test_empty_export_is_free_and_explained(client: AsyncClient, db_session: AsyncSession):
    token, org, headers = await _owner_org(client, "emptyexport", plan_key="growth", db_session=db_session)
    connection = await create_fake_connection(client, token, org["id"])
    params = {
        "connection_id": connection["id"],
        "from_datetime": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        "to_datetime": datetime.now(timezone.utc).isoformat(),
    }
    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export/preview", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["row_count"] == 0

    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
    assert resp.status_code == 404
    assert "no export was used" in resp.json()["detail"]
    usage = (
        await db_session.execute(select(ExportUsage).where(ExportUsage.organization_id == org["id"]))
    ).scalar_one_or_none()
    assert usage is None or usage.count == 0


async def test_export_includes_recorded_contributions_and_uses_credits_after_allowance(
    client: AsyncClient, db_session: AsyncSession
):
    token, org, headers = await _owner_org(client, "exportcredits", plan_key="growth", db_session=db_session)
    connection = await create_fake_connection(client, token, org["id"])
    now = datetime.now(timezone.utc)
    await add_accepted_contribution(
        db_session, organization_id=org["id"], connection_id=connection["id"], received_at=now - timedelta(hours=1),
        amount="40.00", sender_name="Ama Mensah",
    )
    params = {
        "connection_id": connection["id"],
        "from_datetime": (now - timedelta(days=1)).isoformat(),
        "to_datetime": (now + timedelta(minutes=1)).isoformat(),
    }
    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export/preview", params=params, headers=headers)
    assert resp.json()["row_count"] == 1
    assert Decimal(resp.json()["total_amount"]) == Decimal("40.00")
    assert resp.json()["sample_rows"][0][0] == "Ama Mensah"

    for _ in range(3):  # Growth includes 3 a month
        resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        assert "Ama Mensah" in resp.text
        assert resp.headers["X-Export-Rows"] == "1"

    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
    assert resp.status_code == 402
    assert "Buy extra exports" in resp.json()["detail"]

    org_row = await db_session.get(Organization, org["id"])
    org_row.export_credits = 1
    await db_session.commit()
    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    await db_session.refresh(org_row)
    assert org_row.export_credits == 0


async def test_starter_can_export_with_purchased_credits(client: AsyncClient, db_session: AsyncSession):
    token, org, headers = await _owner_org(client, "starterexport")
    connection = await create_fake_connection(client, token, org["id"])
    now = datetime.now(timezone.utc)
    await add_accepted_contribution(
        db_session, organization_id=org["id"], connection_id=connection["id"], received_at=now - timedelta(hours=1)
    )
    params = {
        "connection_id": connection["id"],
        "from_datetime": (now - timedelta(days=1)).isoformat(),
        "to_datetime": now.isoformat(),
    }
    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
    assert resp.status_code == 402
    org_row = await db_session.get(Organization, org["id"])
    org_row.export_credits = 2
    await db_session.commit()
    resp = await client.get(f"/v1/organizations/{org['id']}/contribution-export.csv", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    await db_session.refresh(org_row)
    assert org_row.export_credits == 1


# --- promotions for existing subscribers -----------------------------------------------
async def test_existing_subscriber_claims_promotion_in_one_click(client: AsyncClient, db_session: AsyncSession):
    _, org, headers = await _owner_org(client, "claimpromo")
    growth = await _plan(db_session, "growth")
    fake = use_fake_stripe()
    subscription = await _link_subscription(db_session, fake, org["id"], growth, 700)
    now = datetime.now(timezone.utc)
    db_session.add(
        BillingPromotion(
            name="Thank you",
            percent_off=50,
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=45),
            applies_to_existing=True,
            applies_to_new=False,
            is_active=True,
        )
    )
    await db_session.commit()
    try:
        resp = await client.get(f"/v1/organizations/{org['id']}/billing", headers=headers)
        promo = resp.json()["available_promotion"]
        assert promo["promotion"]["percent_off"] == 50
        assert promo["intro_cycles"] == 1

        resp = await client.post(f"/v1/organizations/{org['id']}/billing/apply-promotion", headers=headers)
        assert resp.status_code == 200, resp.text
    finally:
        stop_fake_stripe()
    body = resp.json()
    assert body["available_promotion"] is None  # can't be claimed twice
    assert Decimal(body["subscription"]["amount"]) == Decimal("3.50")
    assert Decimal(body["subscription"]["regular_amount"]) == Decimal("7.00")
    assert fake.updates[-1][0] == subscription.provider_subscription_id


# --- admin -------------------------------------------------------------------------------------
async def _admin_headers(client: AsyncClient, db_session: AsyncSession, email: str) -> dict[str, str]:
    token = await register_and_login(client, email)
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    await make_platform_admin(db_session, me.json()["id"])
    return {"Authorization": f"Bearer {token}"}


async def test_admin_saves_stripe_keys_without_ever_returning_them(client: AsyncClient, db_session: AsyncSession):
    headers = await _admin_headers(client, db_session, "admin-keys@example.org")
    resp = await client.post(
        "/v1/admin/payment-settings", json={"stripe_secret_key": "pk_test_wrongkind"}, headers=headers
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/v1/admin/payment-settings",
        json={
            "stripe_secret_key": "sk_test_51abcdefghijklmnop1234",
            "stripe_webhook_secret": "whsec_abc",
            "session_topup_price_cents": 250,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "stripe_configured": True,
        "stripe_mode": "test",
        "stripe_key_last4": "1234",
        "webhook_configured": True,
        "session_topup_price_cents": 250,
        "export_topup_price_cents": 100,
    }
    resp = await client.get("/v1/admin/payment-settings", headers=headers)
    assert "sk_test_51abcdefghijklmnop1234" not in resp.text

    settings = await get_billing_settings(db_session)
    await db_session.refresh(settings)
    assert settings.stripe_secret_key == "sk_test_51abcdefghijklmnop1234"  # decrypted transparently


async def test_admin_price_change_reprices_existing_subscriptions_from_next_renewal(
    client: AsyncClient, db_session: AsyncSession
):
    _, org, _ = await _owner_org(client, "reprice")
    growth = await _plan(db_session, "growth")
    headers = await _admin_headers(client, db_session, "admin-reprice@example.org")
    fake = use_fake_stripe()
    await _link_subscription(db_session, fake, org["id"], growth, 700)
    try:
        resp = await client.post(
            f"/v1/admin/plans/{growth.id}/pricing",
            json={"monthly_price_cents": 900, "reason": "More support included."},
            headers=headers,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan"]["monthly_price_cents"] == 900
    assert resp.json()["subscriptions_updated"] == 1
    _, params = fake.updates[-1]
    assert params["items"][0]["price_data"]["unit_amount"] == 900
    assert params["proration_behavior"] == "none"  # nothing charged now


async def test_admin_refund_goes_through_stripe(client: AsyncClient, db_session: AsyncSession):
    _, org, _ = await _owner_org(client, "striperefund")
    growth = await _plan(db_session, "growth")
    headers = await _admin_headers(client, db_session, "admin-striperefund@example.org")
    now = datetime.now(timezone.utc)
    db_session.add(
        BillingPayment(
            organization_id=org["id"],
            provider="stripe",
            provider_payment_id="in_refund",
            provider_capture_id="pi_refund",
            amount=Decimal("30.00"),
            currency="USD",
            status=BillingPaymentStatus.COMPLETED,
            period_start=now - timedelta(days=10),
            period_end=now + timedelta(days=20),
            kind="subscription",
        )
    )
    await db_session.commit()
    fake = use_fake_stripe()
    try:
        resp = await client.post(
            f"/v1/admin/organizations/{org['id']}/refund",
            json={"reason": "Customer canceled mid-cycle."},
            headers=headers,
        )
    finally:
        stop_fake_stripe()
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["amount"]) == Decimal("20.00")
    assert fake.refunds[0]["payment_intent"] == "pi_refund"
    assert fake.refunds[0]["amount_cents"] == 2000


async def test_admin_can_grant_credits(client: AsyncClient, db_session: AsyncSession):
    _, org, _ = await _owner_org(client, "grantcredits")
    headers = await _admin_headers(client, db_session, "admin-grantcredits@example.org")
    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/credits",
        json={"session_credits": 3, "export_credits": 2, "reason": "Sorry for the outage"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_credits"] == 3
    assert resp.json()["export_credits"] == 2
