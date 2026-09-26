from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.billing import (
    BillingPayment,
    BillingPaymentStatus,
    BillingSubscription,
    BillingSubscriptionStatus,
)
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.db.models.session_mailbox_connection import SessionMailboxConnection
from app.integrations.paypal import get_paypal_client
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

    async def refund_capture(self, **kwargs):
        self.refunds.append(kwargs)
        return {"id": "R-TEST", "status": "COMPLETED"}

    async def cancel_subscription(self, provider_subscription_id: str, reason: str) -> None:
        return None


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
