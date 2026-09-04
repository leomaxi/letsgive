from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog


async def record_audit_event(
    db: AsyncSession,
    *,
    action: str,
    target_type: str,
    target_id: str | None = None,
    organization_id: str | None = None,
    actor_user_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        ip_address=ip_address,
    )
    db.add(entry)
    await db.flush()
    return entry
