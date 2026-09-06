from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.notification import Notification, NotificationType


async def notify_user(
    db: AsyncSession,
    *,
    user_id: str,
    organization_id: str,
    type: NotificationType,
    title: str,
    body: str,
    session_id: str | None = None,
) -> Notification:
    """Creates an in-app notification. Doesn't commit -- callers already
    control their own transaction boundary (see request_approval in
    app/api/v1/sessions.py, which commits once after both this and the
    external Notifier call).
    """
    notification = Notification(
        user_id=user_id,
        organization_id=organization_id,
        session_id=session_id,
        type=type,
        title=title,
        body=body,
    )
    db.add(notification)
    return notification
