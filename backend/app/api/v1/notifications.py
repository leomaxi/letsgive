from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.api.v1.schemas import NotificationOut
from app.db.models.notification import Notification
from app.db.models.organization import Organization
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter(prefix="/v1/me/notifications", tags=["notifications"])


def _to_notification_out(notification: Notification, org: Organization) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        organization_id=notification.organization_id,
        organization_name=org.name,
        session_id=notification.session_id,
        type=notification.type,
        title=notification.title,
        body=notification.body,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get("", response_model=list[NotificationOut])
async def list_my_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationOut]:
    query = (
        select(Notification, Organization)
        .join(Organization, Organization.id == Notification.organization_id)
        .where(Notification.user_id == current_user.id)
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    result = await db.execute(query.order_by(Notification.created_at.desc()).limit(limit))
    return [_to_notification_out(n, org) for n, org in result.all()]


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationOut:
    result = await db.execute(
        select(Notification, Organization)
        .join(Organization, Organization.id == Notification.organization_id)
        .where(Notification.id == notification_id, Notification.user_id == current_user.id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    notification, org = row
    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notification)
    return _to_notification_out(notification, org)


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_notifications_read(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Notification).where(
            Notification.user_id == current_user.id, Notification.read_at.is_(None)
        )
    )
    now = datetime.now(timezone.utc)
    for notification in result.scalars().all():
        notification.read_at = now
    await db.commit()
