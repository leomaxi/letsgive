from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_membership
from app.api.v1.schemas import AuditLogOut
from app.db.models.audit_log import AuditLog
from app.db.models.membership import Membership
from app.db.session import get_db

router = APIRouter(prefix="/v1/organizations/{organization_id}/audit-logs", tags=["audit"])


@router.get("", response_model=list[AuditLogOut])
async def list_audit_logs(
    organization_id: str,
    limit: int = 100,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    """Read access to the append-only audit trail (spec 8: 'Auditability').
    Open to any active member, not just Owner/Finance/Auditor -- a team is a
    team; Media should see what happened in their own org too, the same way
    sessions/operator/events/display-templates already work (get_membership
    alone, no extra role check). Nothing here is written through this
    endpoint, so there's no write-side control to weaken.
    """
    result = await db.execute(
        select(AuditLog)
        .where(AuditLog.organization_id == organization_id)
        .order_by(AuditLog.created_at.desc())
        .limit(min(limit, 1000))
    )
    return list(result.scalars().all())
