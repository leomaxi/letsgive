from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc, utcnow
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.domain.audit import record_audit_event


async def revert_expired_plans(db: AsyncSession) -> int:
    """Reverts any organization whose admin-assigned plan_expires_at has
    passed back to the Starter plan (spec: a platform admin can grant a
    tenant a plan for a limited date range; if nobody renews it, it lapses
    rather than silently continuing forever). Called by the background loop
    in app/main.py, same shape as poll_all_imap_connections. Returns the
    number of organizations reverted, mostly useful for tests.
    """
    starter = (await db.execute(select(Plan).where(Plan.key == "starter"))).scalar_one_or_none()
    if starter is None:
        # Seed data missing (shouldn't happen outside a broken local setup) --
        # nothing sane to revert to, so leave expired grants alone rather than
        # nulling out plan_id and breaking entitlement checks outright.
        return 0

    now = utcnow()
    result = await db.execute(select(Organization).where(Organization.plan_expires_at.is_not(None)))
    reverted = 0
    for org in result.scalars().all():
        if ensure_utc(org.plan_expires_at) > now:
            continue
        before = {"plan_id": org.plan_id, "plan_expires_at": org.plan_expires_at.isoformat()}
        org.plan_id = starter.id
        org.plan_starts_at = None
        org.plan_expires_at = None
        await record_audit_event(
            db,
            action="subscription.plan_expired",
            target_type="organization",
            target_id=org.id,
            organization_id=org.id,
            actor_user_id=None,
            before=before,
            after={"plan_id": starter.id},
        )
        reverted += 1

    if reverted:
        await db.commit()
    return reverted
