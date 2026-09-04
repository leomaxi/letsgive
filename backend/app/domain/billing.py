from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.display_template import DisplayTemplate
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.membership import Membership, MembershipStatus
from app.db.models.organization import Organization, SubscriptionStatus
from app.db.models.plan import Plan
from app.db.models.session import Session

CANCELED_DETAIL = (
    "This organization's subscription is canceled. Historical reports remain "
    "available; creating new sessions, connections or templates is disabled."
)


def assert_subscription_active(org: Organization) -> None:
    if org.subscription_status == SubscriptionStatus.CANCELED:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=CANCELED_DETAIL)


def assert_reports_readable(org: Organization) -> None:
    """Historical reports stay readable through the grace period after
    cancellation (spec 13: "a reasonable read-only grace period"), but not
    forever. Distinct from assert_subscription_active, which blocks *new*
    sessions/connections/templates immediately on cancellation regardless of
    the grace period -- this only ever blocks *reading* a report, and only
    once grace_period_ends_at has actually passed.

    If somehow canceled with no grace_period_ends_at set (shouldn't happen --
    cancel_subscription always sets one), reports stay readable rather than
    locking the org out over a data anomaly.
    """
    if (
        org.subscription_status == SubscriptionStatus.CANCELED
        and org.grace_period_ends_at is not None
        and datetime.now(timezone.utc) > org.grace_period_ends_at
    ):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                "This organization's subscription was canceled and its report "
                "grace period has ended. Contact support to reactivate."
            ),
        )


async def assert_can_create_session(db: AsyncSession, org: Organization) -> None:
    """Server-side entitlement check (spec 13) -- a plan's monthly session
    quota isn't just displayed in a UI somewhere, it's actually enforced
    here before the row is created.
    """
    assert_subscription_active(org)
    if org.plan_id is None:
        return
    plan = await db.get(Plan, org.plan_id)
    if plan is None:
        return

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.count(Session.id)).where(
            Session.organization_id == org.id, Session.created_at >= month_start
        )
    )
    count = result.scalar_one()
    if count >= plan.max_sessions_per_month:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Monthly session limit reached for the '{plan.name}' plan "
                f"({plan.max_sessions_per_month}/month). Upgrade to create more."
            ),
        )


async def assert_can_create_connection(db: AsyncSession, org: Organization) -> None:
    assert_subscription_active(org)
    if org.plan_id is None:
        return
    plan = await db.get(Plan, org.plan_id)
    if plan is None:
        return

    result = await db.execute(
        select(func.count(MailboxConnection.id)).where(
            MailboxConnection.organization_id == org.id,
            MailboxConnection.status != ConnectionStatus.REVOKED,
        )
    )
    count = result.scalar_one()
    if count >= plan.max_mailbox_connections:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Mailbox connection limit reached for the '{plan.name}' plan "
                f"({plan.max_mailbox_connections}). Upgrade or revoke an existing connection."
            ),
        )


async def assert_can_create_display_template(db: AsyncSession, org: Organization) -> None:
    assert_subscription_active(org)
    if org.plan_id is None:
        return
    plan = await db.get(Plan, org.plan_id)
    if plan is None:
        return

    result = await db.execute(
        select(func.count(DisplayTemplate.id)).where(DisplayTemplate.organization_id == org.id)
    )
    count = result.scalar_one()
    if count >= plan.max_display_templates:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Display template limit reached for the '{plan.name}' plan "
                f"({plan.max_display_templates}). Upgrade or delete an existing template."
            ),
        )


async def assert_can_add_team_member(db: AsyncSession, org: Organization) -> None:
    """A pending invitation reserves a seat the same as an active member --
    it's occupying a slot that will become active the moment it's accepted,
    so it should count against the limit already, not just once accepted.
    """
    assert_subscription_active(org)
    if org.plan_id is None:
        return
    plan = await db.get(Plan, org.plan_id)
    if plan is None:
        return

    result = await db.execute(
        select(func.count(Membership.id)).where(
            Membership.organization_id == org.id,
            Membership.status != MembershipStatus.SUSPENDED,
        )
    )
    count = result.scalar_one()
    if count >= plan.max_team_members:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"Team member limit reached for the '{plan.name}' plan "
                f"({plan.max_team_members} seats). Upgrade to invite more teammates."
            ),
        )
