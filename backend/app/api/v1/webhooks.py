from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.schemas import WebhookAck, WebhookPayload
from app.api.v1.sessions import broadcast_session_update
from app.db.base import utcnow
from app.db.models.contribution_event import ContributionDecision
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection, MailboxProviderName
from app.db.models.session import Session
from app.db.session import get_db
from app.domain.ingestion import ingest_message
from app.domain.mailbox_providers import get_provider

router = APIRouter(prefix="/v1/providers", tags=["webhooks"])


@router.post("/{provider}/webhook", response_model=WebhookAck)
async def receive_webhook(
    provider: MailboxProviderName,
    payload: WebhookPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_letsgive_signature: str = Header(..., alias="X-LetsGive-Signature"),
) -> WebhookAck:
    connection = await db.get(MailboxConnection, payload.connection_id)
    if connection is None or connection.provider != provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")

    provider_adapter = get_provider(connection.provider)
    raw_body = await request.body()
    if not provider_adapter.verify_webhook_signature(
        connection=connection, raw_body=raw_body, signature=x_letsgive_signature
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature."
        )

    if connection.status != ConnectionStatus.CONNECTED:
        return WebhookAck(status="ignored")

    message = await provider_adapter.fetch_message(
        connection=connection, provider_message_id=payload.provider_message_id
    )
    if message is None:
        return WebhookAck(status="ignored")

    result = await ingest_message(db, connection=connection, message=message)

    connection.last_sync_at = utcnow()
    connection.webhook_health = "ok"
    await db.commit()

    if (
        not result.already_processed
        and result.event.decision == ContributionDecision.ACCEPTED
        and result.event.session_id is not None
    ):
        session = await db.get(Session, result.event.session_id)
        if session is not None:
            await broadcast_session_update(db, session)

    return WebhookAck(
        status="processed",
        decision=result.event.decision,
        already_processed=result.already_processed,
        event_id=result.event.id,
    )
