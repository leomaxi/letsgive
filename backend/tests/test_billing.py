from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import (
    BillingInterval,
    BillingPayment,
    BillingPaymentStatus,
    BillingPromotion,
    BillingSettings,
    BillingSubscription,
    BillingSubscriptionStatus,
)
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.db.models.session_mailbox_connection import SessionMailboxConnection
from app.db.models.audit_log import AuditLog
from app.domain.subscriptions import apply_paypal_webhook, promo_cycle_count
from app.integrations.paypal import PayPalClient, get_paypal_client
from app.main import app
from tests.helpers import (
    add_active_member,
    create_fake_connection,
    create_org,
    create_session,
    enable_mfa,
    make_platform_admin,
    register_and_login,
)


class FakePayPalClient:
    def __init__(self) -> None:
        self.refunds: list[dict] = []
        self.subscriptions: list[dict] = []

    async def refund_capture(self, **kwargs):
        self.refunds.append(kwargs)
        return {"id": "R-TEST", "status": "COMPLETED"}

    fail_cancel = False

    async def cancel_subscription(self, provider_subscription_id: str, reason: str) -> None:
        if self.fail_cancel:
            raise RuntimeError("PayPal request failed (500).")
        self.__dict__.setdefault("canceled", []).append(provider_subscription_id)

    async def create_subscription(self, **kwargs):
        self.subscriptions.append(kwargs)
        return {
            "id": f"I-TEST-{len(self.subscriptions)}",
            "links": [{"rel": "approve", "href": "https://paypal.test/approve"}],
        }

    billing_cycle_overrides = staticmethod(PayPalClient.billing_cycle_overrides)
    subscription_details: dict | None = None

    async def create_product(self, **kwargs):
        self.__dict__.setdefault("products", []).append(kwargs)
        return {"id": "PROD-TEST"}

    async def create_plan(self, **kwargs):
        plans = self.__dict__.setdefault("plans", [])
        plans.append(kwargs)
        return {"id": f"P-AUTO-{len(plans)}"}

    async def revise_subscription(self, provider_subscription_id: str, **kwargs):
        self.__dict__.setdefault("revisions", []).append({"id": provider_subscription_id, **kwargs})
        return {"links": [{"rel": "approve", "href": "https://paypal.test/revise"}]}

    async def get_subscription(self, provider_subscription_id: str):
        return self.subscription_details or {}


async def test_new_org_defaults_to_starter_plan(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-plan@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Plan Org")

    assert org["subscription_status"] == "trialing"
    assert org["plan_id"] is not None


async def test_plans_endpoint_lists_seeded_tiers(client: AsyncClient, db_session: AsyncSession):
    token = await register_and_login(client, "anyone-plans@example.org")
    resp = await client.get("/v1/plans", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    keys = {p["key"] for p in resp.json()}
    assert keys == {"starter", "growth", "premium", "enterprise"}


async def test_session_quota_enforced_for_starter_plan(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-quota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Quota Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-quota@example.org", "media"
    )

    # Starter plan allows 2 sessions/month.
    for _ in range(2):
        await create_session(client, media_token, org["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_mailbox_connection_quota_enforced_for_starter_plan(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-connquota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Conn Quota Org")

    # Starter plan allows 1 mailbox connection.
    await create_fake_connection(client, owner_token, org["id"], mailbox="one@church.org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections",
        json={"provider": "fake", "mailbox": "two@church.org"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_display_template_quota_enforced_for_starter_plan(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-templatequota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Template Quota Org")

    # Starter plan allows 2 display templates.
    for _ in range(2):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/display-templates",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_display_template_duplicate_also_respects_quota(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-dupquota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Dup Quota Org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()
    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text

    # Already at 2/2 -- duplicating a third should be blocked too, same as
    # creating one outright.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}/duplicate",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_team_member_quota_enforced_for_starter_plan(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-seatquota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Seat Quota Org")

    # The owner already occupies 1 of Starter's 5 seats; invite 4 more to
    # fill the rest.
    for i in range(4):
        await register_and_login(client, f"seat{i}@example.org")
        resp = await client.post(
            f"/v1/organizations/{org['id']}/members/invite",
            json={"email": f"seat{i}@example.org", "role": "auditor"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 201, resp.text

    await register_and_login(client, "seat-overflow@example.org")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "seat-overflow@example.org", "role": "auditor"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_declining_invitation_frees_a_seat(client: AsyncClient, db_session: AsyncSession):
    """A pending invitation reserves a seat the same as an active member; on
    a full org, declining one should free it back up rather than leaving the
    org permanently short a seat until someone upgrades.
    """
    owner_token = await register_and_login(client, "owner-freeseat@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Free Seat Org")

    invited_tokens = []
    membership_ids = []
    for i in range(4):
        token = await register_and_login(client, f"freeseat{i}@example.org")
        invited_tokens.append(token)
        resp = await client.post(
            f"/v1/organizations/{org['id']}/members/invite",
            json={"email": f"freeseat{i}@example.org", "role": "auditor"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 201, resp.text
        membership_ids.append(resp.json()["id"])

    # At capacity (5/5): owner + 4 invited.
    await register_and_login(client, "freeseat-overflow@example.org")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "freeseat-overflow@example.org", "role": "auditor"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text

    # Decline one -- frees the seat back up.
    resp = await client.post(
        f"/v1/me/invitations/{membership_ids[0]}/decline",
        headers={"Authorization": f"Bearer {invited_tokens[0]}"},
    )
    assert resp.status_code == 204, resp.text

    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "freeseat-overflow@example.org", "role": "auditor"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_canceled_subscription_blocks_new_sessions_but_not_reports(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-cancel@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Cancel Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-cancel@example.org", "media"
    )

    session = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/cancel",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    canceled = resp.json()
    assert canceled["subscription_status"] == "canceled"
    assert canceled["grace_period_ends_at"] is not None

    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 402

    # Historical data remains readable during the grace period.
    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.status_code == 200


async def test_only_owner_can_cancel_subscription(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-cancelrbac@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Cancel RBAC Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-cancelrbac@example.org", "media"
    )

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/cancel",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_owner_can_switch_plan(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-switch@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Switch Org")
    headers = {"Authorization": f"Bearer {owner_token}"}

    resp = await client.get("/v1/plans", headers=headers)
    plans = {p["key"]: p for p in resp.json()}
    assert org["plan_id"] == plans["starter"]["id"]

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": plans["growth"]["id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan_id"] == plans["growth"]["id"]

    # Switching to the plan already active is a harmless no-op.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": plans["growth"]["id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan_id"] == plans["growth"]["id"]

    # A downgrade doesn't retroactively break anything already created --
    # only new creation is gated (covered by the quota tests above).
    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": plans["starter"]["id"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["plan_id"] == plans["starter"]["id"]


async def test_switch_plan_rejects_unknown_plan_and_non_owner(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-switchrbac@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Switch RBAC Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-switchrbac@example.org", "media"
    )

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": "not-a-real-plan"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 404

    resp = await client.get("/v1/plans", headers={"Authorization": f"Bearer {owner_token}"})
    growth_id = next(p["id"] for p in resp.json() if p["key"] == "growth")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": growth_id},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_cannot_switch_plan_after_cancellation(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-switchcanceled@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Switch Canceled Org")
    headers = {"Authorization": f"Bearer {owner_token}"}

    await client.post(f"/v1/organizations/{org['id']}/subscription/cancel", headers=headers)

    resp = await client.get("/v1/plans", headers=headers)
    growth_id = next(p["id"] for p in resp.json() if p["key"] == "growth")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": growth_id},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_reports_stay_readable_within_grace_period_but_not_after(
    client: AsyncClient, db_session: AsyncSession
):
    """grace_period_ends_at is recorded on cancellation but wasn't enforced
    anywhere -- reports stayed readable indefinitely. Confirms both halves:
    still readable right after cancellation (well within the 30-day grace
    period), and blocked once that period has actually elapsed.
    """
    owner_token = await register_and_login(client, "owner-grace@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Grace Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-grace@example.org", "media"
    )
    headers = {"Authorization": f"Bearer {owner_token}"}
    session = await create_session(client, media_token, org["id"])

    resp = await client.post(f"/v1/organizations/{org['id']}/subscription/cancel", headers=headers)
    assert resp.status_code == 200, resp.text

    # Still within the grace period -- the JSON report, the CSV export, and
    # the PDF export all stay readable, same as before cancellation.
    resp = await client.get(f"/v1/reports/sessions/{session['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/v1/reports/sessions/{session['id']}/export.csv", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/v1/reports/sessions/{session['id']}/export.pdf", headers=headers)
    assert resp.status_code == 200, resp.text

    # Push grace_period_ends_at into the past, simulating 30+ days elapsed
    # (nothing reachable through the API alone advances real time that far).
    result = await db_session.execute(select(Organization).where(Organization.id == org["id"]))
    db_org = result.scalar_one()
    db_org.grace_period_ends_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    resp = await client.get(f"/v1/reports/sessions/{session['id']}", headers=headers)
    assert resp.status_code == 402, resp.text
    resp = await client.get(f"/v1/reports/sessions/{session['id']}/export.csv", headers=headers)
    assert resp.status_code == 402, resp.text
    resp = await client.get(f"/v1/reports/sessions/{session['id']}/export.pdf", headers=headers)
    assert resp.status_code == 402, resp.text

    # Out of scope for this cutoff: the live operator console (status,
    # ledger events) isn't a "historical report" and stays reachable --
    # matches how the README describes this fix (reports specifically).
    resp = await client.get(f"/v1/sessions/{session['id']}/operator", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/v1/sessions/{session['id']}/events", headers=headers)
    assert resp.status_code == 200, resp.text


async def test_admin_refund_requires_reason(client: AsyncClient, db_session: AsyncSession):
    admin_token = await register_and_login(client, "admin-refund-reason@example.org")
    user_result = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    await make_platform_admin(db_session, user_result.json()["id"])

    resp = await client.post(
        "/v1/admin/organizations/not-real/refund",
        json={"reason": ""},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422


async def test_admin_processes_prorated_paypal_refund(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-refund@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Refund Org")

    admin_token = await register_and_login(client, "admin-refund@example.org")
    admin_resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    await make_platform_admin(db_session, admin_resp.json()["id"])

    plan_result = await db_session.execute(select(Plan).where(Plan.key == "growth"))
    plan = plan_result.scalar_one()
    subscription = BillingSubscription(
        organization_id=org["id"],
        plan_id=plan.id,
        provider_subscription_id="I-TEST",
        status=BillingSubscriptionStatus.ACTIVE,
        currency="CAD",
        amount=Decimal("29.00"),
    )
    db_session.add(subscription)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    payment = BillingPayment(
        organization_id=org["id"],
        billing_subscription_id=subscription.id,
        provider_payment_id="SALE-TEST",
        provider_capture_id="CAPTURE-TEST",
        amount=Decimal("30.00"),
        currency="CAD",
        status=BillingPaymentStatus.COMPLETED,
        period_start=now - timedelta(days=10),
        period_end=now + timedelta(days=20),
    )
    db_session.add(payment)
    await db_session.commit()

    fake_paypal = FakePayPalClient()
    app.dependency_overrides[get_paypal_client] = lambda: fake_paypal
    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/refund",
        json={"reason": "Customer canceled mid-cycle."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    app.dependency_overrides.pop(get_paypal_client, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_refund_id"] == "R-TEST"
    assert body["status"] == "completed"
    assert Decimal(body["amount"]) == Decimal("20.00")
    assert fake_paypal.refunds[0]["capture_id"] == "CAPTURE-TEST"
    assert fake_paypal.refunds[0]["amount"] == Decimal("20.00")


async def test_starter_plan_cannot_export_transactions(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-noexport@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "No Export Org")
    connection = await create_fake_connection(client, owner_token, org["id"])

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export.csv",
        params={
            "connection_id": connection["id"],
            "from_datetime": datetime.now(timezone.utc).isoformat(),
            "to_datetime": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_growth_export_quota_is_three_per_month(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-exportquota@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Export Quota Org")
    plan_result = await db_session.execute(select(Plan).where(Plan.key == "growth"))
    org_result = await db_session.execute(select(Organization).where(Organization.id == org["id"]))
    org_row = org_result.scalar_one()
    org_row.plan_id = plan_result.scalar_one().id
    await db_session.commit()
    connection = await create_fake_connection(client, owner_token, org["id"])
    params = {
        "connection_id": connection["id"],
        "from_datetime": datetime.now(timezone.utc).isoformat(),
        "to_datetime": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    }

    for _ in range(3):
        resp = await client.get(
            f"/v1/organizations/{org['id']}/contribution-export.csv",
            params=params,
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export.csv",
        params=params,
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_premium_allows_two_mailboxes_per_session(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-twomail@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Two Mail Org")
    plan_result = await db_session.execute(select(Plan).where(Plan.key == "premium"))
    org_result = await db_session.execute(select(Organization).where(Organization.id == org["id"]))
    org_row = org_result.scalar_one()
    org_row.plan_id = plan_result.scalar_one().id
    await db_session.commit()
    first = await create_fake_connection(client, owner_token, org["id"], mailbox="one@example.org")
    second = await create_fake_connection(client, owner_token, org["id"], mailbox="two@example.org")

    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
            "mailbox_connection_ids": [first["id"], second["id"]],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text
    assert set(resp.json()["mailbox_connection_ids"]) == {first["id"], second["id"]}

    rows = await db_session.execute(
        select(SessionMailboxConnection).where(SessionMailboxConnection.session_id == resp.json()["id"])
    )
    assert len(rows.scalars().all()) == 2


async def test_growth_blocks_two_mailboxes_per_session(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-onemail@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "One Mail Org")
    plan_result = await db_session.execute(select(Plan).where(Plan.key == "growth"))
    org_result = await db_session.execute(select(Organization).where(Organization.id == org["id"]))
    org_row = org_result.scalar_one()
    org_row.plan_id = plan_result.scalar_one().id
    await db_session.commit()
    first = await create_fake_connection(client, owner_token, org["id"], mailbox="one@example.org")
    second = await create_fake_connection(client, owner_token, org["id"], mailbox="two@example.org")

    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
            "mailbox_connection_ids": [first["id"], second["id"]],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 402, resp.text


async def test_admin_bonus_sessions_must_expire_within_subscription_period(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-bonus@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Bonus Org")
    admin_token = await register_and_login(client, "admin-bonus@example.org")
    admin_resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    await make_platform_admin(db_session, admin_resp.json()["id"])

    plan = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    subscription = BillingSubscription(
        organization_id=org["id"],
        plan_id=plan.id,
        provider_subscription_id="I-BONUS",
        status=BillingSubscriptionStatus.ACTIVE,
        currency="USD",
        amount=Decimal("7.00"),
        interval=BillingInterval.MONTHLY,
        current_period_start=datetime.now(timezone.utc),
        current_period_end=datetime.now(timezone.utc) + timedelta(days=20),
    )
    db_session.add(subscription)
    await db_session.commit()

    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/bonus-sessions",
        json={
            "bonus_sessions": 3,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 400, resp.text

    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/bonus-sessions",
        json={
            "bonus_sessions": 3,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["bonus_sessions"] == 3


async def test_admin_price_change_notifies_plan_owners(
    client: AsyncClient, db_session: AsyncSession, notifier
):
    owner_token = await register_and_login(client, "owner-price@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Price Org")
    plan = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    org_row = (await db_session.execute(select(Organization).where(Organization.id == org["id"]))).scalar_one()
    org_row.plan_id = plan.id
    await db_session.commit()

    admin_token = await register_and_login(client, "admin-price@example.org")
    admin_resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    await make_platform_admin(db_session, admin_resp.json()["id"])

    resp = await client.post(
        f"/v1/admin/plans/{plan.id}/pricing",
        json={"monthly_price_cents": 900, "reason": "More included support."},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["monthly_price_cents"] == 900
    assert any("More included support." in entry.get("body", "") for entry in notifier.sent)


async def test_yearly_subscription_applies_admin_savings_and_promotion(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-yearly@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Yearly Org")
    plan = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    plan.paypal_yearly_plan_id = "P-GROWTH-YEARLY"
    db_session.add(BillingSettings(key="default", annual_savings_percent=10))
    db_session.add(
        BillingPromotion(
            name="Launch",
            percent_off=20,
            starts_at=datetime.now(timezone.utc) - timedelta(days=1),
            ends_at=datetime.now(timezone.utc) + timedelta(days=30),
            plan_id=plan.id,
            applies_to_existing=True,
            applies_to_new=True,
            is_active=True,
        )
    )
    await db_session.commit()

    fake_paypal = FakePayPalClient()
    app.dependency_overrides[get_paypal_client] = lambda: fake_paypal
    resp = await client.post(
        f"/v1/organizations/{org['id']}/billing/paypal-subscription",
        json={"plan_id": plan.id, "interval": "yearly"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    app.dependency_overrides.pop(get_paypal_client, None)

    assert resp.status_code == 200, resp.text
    sent = fake_paypal.subscriptions[0]
    assert sent["plan_id"] == "P-GROWTH-YEARLY"
    # Growth USD 7 * 12 = 84; 10% annual savings -> 75.60; 20% promo -> 60.48
    # for the one yearly charge inside the promo window, then back to 75.60.
    intro, regular = sent["billing_cycles"]
    assert intro["pricing_scheme"]["fixed_price"]["value"] == "60.48"
    assert intro["total_cycles"] == 1
    assert regular["pricing_scheme"]["fixed_price"]["value"] == "75.60"
    assert regular["total_cycles"] == 0


async def _owner_with_org(client: AsyncClient, prefix: str):
    owner_token = await register_and_login(client, f"owner-{prefix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"{prefix} Org")
    return owner_token, org


async def test_checkout_auto_creates_paypal_plans_when_unmapped(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_with_org(client, "autoplan")
    plan = (await db_session.execute(select(Plan).where(Plan.key == "premium"))).scalar_one()
    assert plan.paypal_monthly_plan_id is None

    fake_paypal = FakePayPalClient()
    app.dependency_overrides[get_paypal_client] = lambda: fake_paypal
    try:
        for _ in range(2):
            resp = await client.post(
                f"/v1/organizations/{org['id']}/billing/paypal-subscription",
                json={"plan_id": plan.id},
                headers={"Authorization": f"Bearer {owner_token}", "Origin": "https://letsgive.ca"},
            )
            assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.pop(get_paypal_client, None)

    # PayPal returns the payer to the site they started from, never localhost.
    assert fake_paypal.subscriptions[0]["return_url"] == "https://letsgive.ca/billing?paypal=approved"
    assert fake_paypal.subscriptions[0]["cancel_url"] == "https://letsgive.ca/billing?paypal=canceled"
    # One product and one monthly plan, reused by the second checkout.
    assert len(fake_paypal.products) == 1
    assert len(fake_paypal.plans) == 1
    assert fake_paypal.plans[0]["interval"] == "monthly"
    assert fake_paypal.plans[0]["price"] == Decimal("13.00")
    await db_session.refresh(plan)
    assert plan.paypal_monthly_plan_id == "P-AUTO-1"
    # No promotion: a single intro cycle at the regular price.
    intro, regular = fake_paypal.subscriptions[0]["billing_cycles"]
    assert intro == {"sequence": 1, "total_cycles": 1, "pricing_scheme": regular["pricing_scheme"]}


def test_promo_cycle_count_counts_charges_inside_window():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    promo = BillingPromotion(
        name="Spring",
        percent_off=25,
        starts_at=now - timedelta(days=1),
        ends_at=datetime(2026, 3, 20, tzinfo=timezone.utc),
    )
    # Jan 10, Feb 10, Mar 10 fall inside; Apr 10 does not.
    assert promo_cycle_count(now, promo, BillingInterval.MONTHLY) == 3
    assert promo_cycle_count(now, promo, BillingInterval.YEARLY) == 1
    later = datetime(2026, 4, 1, tzinfo=timezone.utc)
    assert promo_cycle_count(later, promo, BillingInterval.MONTHLY) == 0


async def test_pricing_endpoint_shows_yearly_savings(client: AsyncClient, db_session: AsyncSession):
    owner_token, org = await _owner_with_org(client, "pricing")
    resp = await client.get(
        f"/v1/organizations/{org['id']}/billing/pricing",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["annual_savings_percent"] == 8
    plan = (await db_session.execute(select(Plan).where(Plan.key == "premium"))).scalar_one()
    yearly = next(q for q in body["quotes"] if q["plan_id"] == plan.id and q["interval"] == "yearly")
    # 13 * 12 = 156; 8% off -> 143.52; saving 12.48.
    assert Decimal(yearly["regular_amount"]) == Decimal("143.52")
    assert Decimal(yearly["savings_amount"]) == Decimal("12.48")


async def test_yearly_payment_webhook_sets_one_year_period(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_with_org(client, "yearwebhook")
    plan = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    subscription = BillingSubscription(
        organization_id=org["id"],
        plan_id=plan.id,
        provider_subscription_id="I-YEARLY",
        status=BillingSubscriptionStatus.ACTIVE,
        currency="USD",
        amount=Decimal("77.28"),
        interval=BillingInterval.YEARLY,
    )
    db_session.add(subscription)
    await db_session.commit()

    await apply_paypal_webhook(
        db_session,
        {
            "event_type": "PAYMENT.SALE.COMPLETED",
            "resource": {
                "id": "SALE-1",
                "billing_agreement_id": "I-YEARLY",
                "amount": {"total": "77.28", "currency": "USD"},
            },
        },
    )
    await db_session.commit()
    await db_session.refresh(subscription)
    days = (subscription.current_period_end - subscription.current_period_start).days
    assert 365 <= days <= 366


async def test_existing_subscriber_can_approve_new_promotion_pricing(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_with_org(client, "revise")
    plan = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    plan.paypal_monthly_plan_id = "P-GROWTH-MONTHLY"
    now = datetime.now(timezone.utc)
    subscription = BillingSubscription(
        organization_id=org["id"],
        plan_id=plan.id,
        provider_subscription_id="I-REVISE",
        status=BillingSubscriptionStatus.ACTIVE,
        currency="USD",
        amount=Decimal("7.00"),
        regular_amount=Decimal("7.00"),
        interval=BillingInterval.MONTHLY,
        current_period_start=now,
        current_period_end=now + timedelta(days=10),
    )
    db_session.add(subscription)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {owner_token}"}

    # No promotion yet: nothing to approve.
    overview = await client.get(f"/v1/organizations/{org['id']}/billing", headers=headers)
    assert overview.json()["pricing_update"] is None

    db_session.add(
        BillingPromotion(
            name="Existing thanks",
            percent_off=50,
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=20),
            applies_to_existing=True,
            applies_to_new=False,
            is_active=True,
        )
    )
    await db_session.commit()

    overview = await client.get(f"/v1/organizations/{org['id']}/billing", headers=headers)
    update = overview.json()["pricing_update"]
    assert Decimal(update["intro_amount"]) == Decimal("3.50")
    assert update["intro_cycles"] == 1

    fake_paypal = FakePayPalClient()
    app.dependency_overrides[get_paypal_client] = lambda: fake_paypal
    try:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/paypal-subscription/revise", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["approval_url"] == "https://paypal.test/revise"
        assert fake_paypal.revisions[0]["id"] == "I-REVISE"

        fake_paypal.subscription_details = {
            "plan": {"billing_cycles": fake_paypal.revisions[0]["billing_cycles"]}
        }
        resp = await client.post(
            f"/v1/organizations/{org['id']}/billing/paypal-subscription/revise/confirm",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.pop(get_paypal_client, None)

    body = resp.json()
    assert Decimal(body["subscription"]["amount"]) == Decimal("3.50")
    assert Decimal(body["subscription"]["regular_amount"]) == Decimal("7.00")
    assert body["pricing_update"] is None


async def test_admin_can_deactivate_promotion(client: AsyncClient, db_session: AsyncSession):
    admin_token = await register_and_login(client, "admin-promo@example.org")
    admin_resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    await make_platform_admin(db_session, admin_resp.json()["id"])
    headers = {"Authorization": f"Bearer {admin_token}"}
    now = datetime.now(timezone.utc)
    resp = await client.post(
        "/v1/admin/promotions",
        json={
            "name": "Holiday",
            "percent_off": 15,
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(days=30)).isoformat(),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.patch(
        f"/v1/admin/promotions/{resp.json()['id']}", json={"is_active": False}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


async def _switch_setup(client: AsyncClient, db_session: AsyncSession, prefix: str):
    """An org on an active Growth monthly subscription that has just started
    checkout for Premium yearly (approval pending)."""
    _, org = await _owner_with_org(client, prefix)
    growth = (await db_session.execute(select(Plan).where(Plan.key == "growth"))).scalar_one()
    premium = (await db_session.execute(select(Plan).where(Plan.key == "premium"))).scalar_one()
    now = datetime.now(timezone.utc)
    old = BillingSubscription(
        organization_id=org["id"],
        plan_id=growth.id,
        provider_subscription_id=f"I-OLD-{prefix}",
        status=BillingSubscriptionStatus.ACTIVE,
        currency="USD",
        amount=Decimal("7.00"),
        interval=BillingInterval.MONTHLY,
        created_at=now - timedelta(days=10),
    )
    new = BillingSubscription(
        organization_id=org["id"],
        plan_id=premium.id,
        provider_subscription_id=f"I-NEW-{prefix}",
        status=BillingSubscriptionStatus.APPROVAL_PENDING,
        currency="USD",
        amount=Decimal("143.52"),
        interval=BillingInterval.YEARLY,
        created_at=now,
    )
    db_session.add_all([old, new])
    org_row = await db_session.get(Organization, org["id"])
    org_row.plan_id = growth.id
    await db_session.commit()
    return org_row, old, new, growth, premium


def _event(event_type: str, resource: dict) -> dict:
    return {"event_type": event_type, "resource": resource}


async def test_activating_new_subscription_cancels_the_old_one(
    client: AsyncClient, db_session: AsyncSession
):
    org, old, new, growth, premium = await _switch_setup(client, db_session, "switch")
    fake_paypal = FakePayPalClient()

    await apply_paypal_webhook(
        db_session, _event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": new.provider_subscription_id}), fake_paypal
    )
    await db_session.commit()
    assert fake_paypal.canceled == [old.provider_subscription_id]
    await db_session.refresh(old)
    await db_session.refresh(org)
    assert old.status == BillingSubscriptionStatus.CANCELED
    assert org.plan_id == premium.id

    # PayPal then echoes the cancellation of the old subscription, and a
    # renewal already in flight for it lands late: neither may touch the org.
    await apply_paypal_webhook(
        db_session, _event("BILLING.SUBSCRIPTION.CANCELLED", {"id": old.provider_subscription_id}), fake_paypal
    )
    await apply_paypal_webhook(
        db_session,
        _event(
            "PAYMENT.SALE.COMPLETED",
            {
                "id": "SALE-LATE",
                "billing_agreement_id": old.provider_subscription_id,
                "amount": {"total": "7.00", "currency": "USD"},
            },
        ),
        fake_paypal,
    )
    await db_session.commit()
    await db_session.refresh(org)
    assert org.subscription_status.value == "active"
    assert org.grace_period_ends_at is None
    assert org.plan_id == premium.id
    # The late payment is still recorded (so it can be refunded).
    payment = (
        await db_session.execute(select(BillingPayment).where(BillingPayment.provider_payment_id == "SALE-LATE"))
    ).scalar_one()
    assert payment.billing_subscription_id == old.id


async def test_failed_cancel_of_old_subscription_is_audited_not_hidden(
    client: AsyncClient, db_session: AsyncSession
):
    org, old, new, growth, premium = await _switch_setup(client, db_session, "cancelfail")
    fake_paypal = FakePayPalClient()
    fake_paypal.fail_cancel = True

    await apply_paypal_webhook(
        db_session, _event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": new.provider_subscription_id}), fake_paypal
    )
    await db_session.commit()
    await db_session.refresh(old)
    await db_session.refresh(org)
    assert old.status == BillingSubscriptionStatus.ACTIVE
    assert org.plan_id == premium.id
    audit = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "paypal.superseded_subscription_cancel_failed")
        )
    ).scalar_one()
    assert audit.target_id == old.id


async def test_late_approval_of_older_checkout_is_canceled_not_applied(
    client: AsyncClient, db_session: AsyncSession
):
    org, old, new, growth, premium = await _switch_setup(client, db_session, "lateapprove")
    # The org already moved to the newer subscription; the older checkout
    # (pending) gets approved afterwards.
    new.status = BillingSubscriptionStatus.ACTIVE
    old.status = BillingSubscriptionStatus.APPROVAL_PENDING
    org.plan_id = premium.id
    await db_session.commit()
    fake_paypal = FakePayPalClient()

    await apply_paypal_webhook(
        db_session, _event("BILLING.SUBSCRIPTION.ACTIVATED", {"id": old.provider_subscription_id}), fake_paypal
    )
    await db_session.commit()
    await db_session.refresh(old)
    await db_session.refresh(org)
    assert fake_paypal.canceled == [old.provider_subscription_id]
    assert old.status == BillingSubscriptionStatus.CANCELED
    assert org.plan_id == premium.id
