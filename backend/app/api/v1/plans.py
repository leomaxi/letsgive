from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.api.v1.schemas import PlanOut
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/v1/plans", tags=["plans"])


@router.get("", response_model=list[PlanOut])
async def list_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Plan]:
    result = await db.execute(select(Plan).order_by(Plan.max_sessions_per_month))
    return list(result.scalars().all())
