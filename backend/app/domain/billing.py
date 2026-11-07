from datetime import datetime, timezone

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.display_template import DisplayTemplate
from app.db.models.export_usage import ExportUsage
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.membership import Membership, MembershipStatus, Role
from app.db.models.notification import NotificationType
from app.db.models.organization import Organization, SubscriptionStatus
from app.db.models.plan import Plan
from app.db.models.session import Session

WATERMARK_TEXT = "Powered by LetsGive.ca"

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


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month_start(now: datetime) -> datetime:
    start = _month_start(now)
    return start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)


def active_bonus_sessions(org: Organization, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if (
        org.bonus_sessions > 0
        and org.bonus_sessions_expires_at is not None
        and org.bonus_sessions_expires_at > now
    ):
        return org.bonus_sessions
    return 0


async def sessions_used_this_month(db: AsyncSession, org: Organization, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(func.count(Session.id)).where(
            Session.organization_id == org.id, Session.created_at >= _month_start(now)
        )
    )
    return result.scalar_one()


async def exports_used_this_month(db: AsyncSession, org: Organization, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(ExportUsage.count).where(
            ExportUsage.organization_id == org.id, ExportUsage.month == now.strftime("%Y-%m")
        )
    )
    return result.scalar_one_or_none() or 0


@dataclass
class UsageSummary:
    sessions_used: int
    sessions_included: int | None  # plan allowance + active bonus; None = no plan limit
    session_credits: int
    exports_used: int
    exports_included: int | None  # None = unlimited
    export_credits: int
    resets_at: datetime


async def usage_summary(db: AsyncSession, org: Organization) -> UsageSummary:
    now = datetime.now(timezone.utc)
    plan = await get_org_plan(db, org)
    return UsageSummary(
        sessions_used=await sessions_used_this_month(db, org, now),
        sessions_included=(plan.max_sessions_per_month + active_bonus_sessions(org, now)) if plan else None,
        session_credits=org.session_credits,
        exports_used=await exports_used_this_month(db, org, now),
        exports_included=plan.max_exports_per_month if plan else 0,
        export_credits=org.export_credits,
        resets_at=_next_month_start(now),
    )


async def _take_credit(db: AsyncSession, org: Organization, attribute: str) -> bool:
    """Atomically uses one purchased credit. A conditional UPDATE rather
    than read-modify-write, so two simultaneous requests can never spend the
    same last credit."""
    column = getattr(Organization, attribute)
    result = await db.execute(
        update(Organization)
        .where(Organization.id == org.id, column > 0)
        .values({attribute: column - 1})
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    await db.refresh(org, attribute_names=[attribute])
    return True


async def _notify_billing_contacts(db: AsyncSession, org: Organization, *, title: str, body: str) -> None:
    """In-app notice to Owners and Finance, plus an email to Owners."""
    from app.domain.inbox import notify_user
    from app.domain.notifications import get_notifier
    from app.db.models.user import User

    rows = await db.execute(
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.organization_id == org.id,
            Membership.status == MembershipStatus.ACTIVE,
            Membership.role.in_([Role.OWNER, Role.FINANCE]),
        )
    )
    for user, role in rows.all():
        await notify_user(
            db,
            user_id=user.id,
            organization_id=org.id,
            type=NotificationType.BILLING_NOTICE,
            title=title,
            body=body,
        )
        if role == Role.OWNER:
            try:
                await get_notifier().send_billing_notice(
                    to_email=user.email, subject=f"Let's Give: {title}", body=body
                )
            except Exception:  # noqa: BLE001 -- a mail hiccup must not block the action
                pass


async def assert_can_create_session(db: AsyncSession, org: Organization) -> None:
    """Server-side entitlement check (spec 13) -- a plan's monthly session
    quota isn't just displayed in a UI somewhere, it's actually enforced
    here before the row is created. Once the month's allowance (plan + any
    admin bonus) is used up, a purchased session credit is spent instead;
    credits never expire. Must be called inside the same transaction that
    creates the session, so a failed create rolls the credit back too.
    """
    assert_subscription_active(org)
    if org.plan_id is None:
        return
    plan = await db.get(Plan, org.plan_id)
    if plan is None:
        return

    count = await sessions_used_this_month(db, org)
    allowed_sessions = plan.max_sessions_per_month + active_bonus_sessions(org)
    if count < allowed_sessions:
        if count + 1 == allowed_sessions and org.session_credits == 0:
            await _notify_billing_contacts(
                db,
                org,
                title="Last included session this month",
                body=(
                    f"You've now used all {allowed_sessions} sessions included in your "
                    f"{plan.name} plan this month. To run more before the month resets, buy "
                    "extra sessions (they never expire) or upgrade on the Billing page."
                ),
            )
        return

    if await _take_credit(db, org, "session_credits"):
        if org.session_credits == 0:
            await _notify_billing_contacts(
                db,
                org,
                title="Last extra session used",
                body=(
                    "You've used your last purchased extra session. Buy more on the "
                    "Billing page if you need more sessions this month."
                ),
            )
        return

    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=(
            f"You've used all {allowed_sessions} sessions included in the '{plan.name}' plan "
            "this month. Buy extra sessions (they never expire) or upgrade on the Billing page."
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


async def get_org_plan(db: AsyncSession, org: Organization) -> Plan | None:
    if org.plan_id is None:
        return None
    return await db.get(Plan, org.plan_id)


async def plan_requires_watermark(db: AsyncSession, org: Organization) -> bool:
    plan = await get_org_plan(db, org)
    return plan is None or plan.key == "starter"


async def assert_session_mailbox_limit(
    db: AsyncSession, org: Organization, mailbox_connection_ids: list[str]
) -> None:
    plan = await get_org_plan(db, org)
    if plan is None or plan.max_session_mailbox_connections is None:
        return
    if len(mailbox_connection_ids) > plan.max_session_mailbox_connections:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"The '{plan.name}' plan allows {plan.max_session_mailbox_connections} "
                "connected mailbox per session. Upgrade to monitor multiple emails in one session."
            ),
        )


async def assert_can_export(db: AsyncSession, org: Organization) -> None:
    """Cheap pre-check before an export does slow mailbox work: is there an
    included export or a purchased export credit left? Doesn't use one --
    see consume_export_quota, which runs only once the export has rows."""
    plan = await get_org_plan(db, org)
    if plan is not None and plan.max_exports_per_month is None:
        return
    included = plan.max_exports_per_month if plan else 0
    if await exports_used_this_month(db, org) < included or org.export_credits > 0:
        return
    if included == 0:
        detail = (
            "Your plan doesn't include transaction exports. Buy export credits or "
            "upgrade on the Billing page."
        )
    else:
        detail = (
            f"You've used all {included} exports included in the '{plan.name}' plan this "
            "month. Buy extra exports (they never expire) or upgrade on the Billing page."
        )
    raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=detail)


async def consume_export_quota(db: AsyncSession, org: Organization) -> str:
    """Uses one export: from the month's allowance first, then a purchased
    credit. Returns "included", "credit" or "unlimited". Callers must only
    call this for an export that actually produced rows -- an empty export
    must never cost the organization anything."""
    plan = await get_org_plan(db, org)
    if plan is not None and plan.max_exports_per_month is None:
        return "unlimited"
    included = plan.max_exports_per_month if plan else 0

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    result = await db.execute(
        select(ExportUsage).where(ExportUsage.organization_id == org.id, ExportUsage.month == month)
    )
    usage = result.scalar_one_or_none()
    if usage is None:
        usage = ExportUsage(organization_id=org.id, month=month, count=0)
        db.add(usage)
        await db.flush()

    if usage.count < included:
        usage.count += 1
        if usage.count == included and org.export_credits == 0:
            await _notify_billing_contacts(
                db,
                org,
                title="Last included export this month",
                body=(
                    f"You've used all {included} exports included in your plan this month. "
                    "Buy extra exports (they never expire) or upgrade on the Billing page."
                ),
            )
        return "included"

    if await _take_credit(db, org, "export_credits"):
        return "credit"
    await assert_can_export(db, org)  # raises the right 402 message
    raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="No exports left.")
