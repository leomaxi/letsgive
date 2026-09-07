from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_membership
from app.api.v1.schemas import (
    SupportTicketCreateRequest,
    SupportTicketDetailOut,
    SupportTicketMessageCreateRequest,
    SupportTicketMessageOut,
    SupportTicketOut,
)
from app.db.models.membership import Membership
from app.db.models.support_ticket import SupportTicket
from app.db.models.support_ticket_message import SupportTicketMessage
from app.db.session import get_db

router = APIRouter(
    prefix="/v1/organizations/{organization_id}/support-tickets", tags=["support"]
)


async def _get_org_ticket(db: AsyncSession, organization_id: str, ticket_id: str) -> SupportTicket:
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None or ticket.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    return ticket


@router.post("", response_model=SupportTicketDetailOut, status_code=status.HTTP_201_CREATED)
async def create_ticket(
    organization_id: str,
    payload: SupportTicketCreateRequest,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> SupportTicketDetailOut:
    """Open to any active member, not just Owner/Finance -- asking for help
    isn't a destructive or financial action, same reasoning as the existing
    team-wide read access to audit logs/mailbox/parser profiles.
    """
    ticket = SupportTicket(
        organization_id=organization_id,
        created_by_user_id=membership.user_id,
        subject=payload.subject,
    )
    db.add(ticket)
    await db.flush()

    message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=membership.user_id,
        author_is_admin=False,
        body=payload.body,
    )
    db.add(message)
    await db.commit()
    await db.refresh(ticket)
    await db.refresh(message)
    return SupportTicketDetailOut(
        **SupportTicketOut.model_validate(ticket).model_dump(),
        messages=[SupportTicketMessageOut.model_validate(message)],
    )


@router.get("", response_model=list[SupportTicketOut])
async def list_tickets(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[SupportTicket]:
    result = await db.execute(
        select(SupportTicket)
        .where(SupportTicket.organization_id == organization_id)
        .order_by(SupportTicket.updated_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{ticket_id}", response_model=SupportTicketDetailOut)
async def get_ticket(
    organization_id: str,
    ticket_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> SupportTicketDetailOut:
    ticket = await _get_org_ticket(db, organization_id, ticket_id)
    result = await db.execute(
        select(SupportTicketMessage)
        .where(SupportTicketMessage.ticket_id == ticket.id)
        .order_by(SupportTicketMessage.created_at.asc())
    )
    messages = list(result.scalars().all())
    return SupportTicketDetailOut(
        **SupportTicketOut.model_validate(ticket).model_dump(),
        messages=[SupportTicketMessageOut.model_validate(m) for m in messages],
    )


@router.post("/{ticket_id}/messages", response_model=SupportTicketMessageOut, status_code=status.HTTP_201_CREATED)
async def reply_to_ticket(
    organization_id: str,
    ticket_id: str,
    payload: SupportTicketMessageCreateRequest,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> SupportTicketMessage:
    ticket = await _get_org_ticket(db, organization_id, ticket_id)
    message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=membership.user_id,
        author_is_admin=False,
        body=payload.body,
    )
    db.add(message)
    # A tenant reply always means admin support hasn't seen this yet.
    ticket.admin_unread = True
    await db.commit()
    await db.refresh(message)
    return message
