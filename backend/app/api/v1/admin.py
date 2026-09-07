from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_platform_admin
from app.api.v1.schemas import (
    AdminOrganizationDetailOut,
    AdminOrganizationOut,
    AdminSubscriptionUpdateRequest,
    AdminSupportTicketDetailOut,
    AdminSupportTicketOut,
    AuditLogOut,
    SupportTicketMessageCreateRequest,
    SupportTicketMessageOut,
    SupportTicketOut,
    SupportTicketStatusUpdateRequest,
)
from app.db.models.audit_log import AuditLog
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.membership import Membership, MembershipStatus
from app.db.models.notification import NotificationType
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.db.models.support_ticket import SupportTicket, SupportTicketStatus
from app.db.models.support_ticket_message import SupportTicketMessage
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.inbox import notify_user

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/organizations", response_model=list[AdminOrganizationOut])
async def list_organizations(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminOrganizationOut]:
    member_counts = (
        select(
            Membership.organization_id.label("organization_id"),
            func.count(Membership.id).label("member_count"),
        )
        .where(Membership.status == MembershipStatus.ACTIVE)
        .group_by(Membership.organization_id)
        .subquery()
    )
    query = (
        select(Organization, Plan.key, Plan.name, member_counts.c.member_count)
        .outerjoin(Plan, Plan.id == Organization.plan_id)
        .outerjoin(member_counts, member_counts.c.organization_id == Organization.id)
        .order_by(Organization.created_at.desc())
    )
    if search:
        # Matches either the org's own name, or a current member's email --
        # plans belong to organizations in this app's data model, so "find
        # the user, assign them a plan" means finding their org first.
        member_email_match = exists(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(Membership.organization_id == Organization.id, User.email.ilike(f"%{search}%"))
        )
        query = query.where(or_(Organization.name.ilike(f"%{search}%"), member_email_match))
    result = await db.execute(query.limit(limit).offset(offset))

    return [
        AdminOrganizationOut(
            id=org.id,
            name=org.name,
            plan_id=org.plan_id,
            plan_key=plan_key,
            plan_name=plan_name,
            subscription_status=org.subscription_status,
            plan_starts_at=org.plan_starts_at,
            plan_expires_at=org.plan_expires_at,
            member_count=member_count or 0,
            created_at=org.created_at,
        )
        for org, plan_key, plan_name, member_count in result.all()
    ]


@router.get("/organizations/{organization_id}", response_model=AdminOrganizationDetailOut)
async def get_organization(
    organization_id: str,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminOrganizationDetailOut:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    plan = await db.get(Plan, org.plan_id) if org.plan_id else None

    member_count_result = await db.execute(
        select(func.count(Membership.id)).where(
            Membership.organization_id == organization_id,
            Membership.status == MembershipStatus.ACTIVE,
        )
    )
    connections_total_result = await db.execute(
        select(func.count(MailboxConnection.id)).where(
            MailboxConnection.organization_id == organization_id
        )
    )
    connections_connected_result = await db.execute(
        select(func.count(MailboxConnection.id)).where(
            MailboxConnection.organization_id == organization_id,
            MailboxConnection.status == ConnectionStatus.CONNECTED,
        )
    )

    return AdminOrganizationDetailOut(
        id=org.id,
        name=org.name,
        plan_id=org.plan_id,
        plan_key=plan.key if plan else None,
        plan_name=plan.name if plan else None,
        subscription_status=org.subscription_status,
        plan_starts_at=org.plan_starts_at,
        plan_expires_at=org.plan_expires_at,
        member_count=member_count_result.scalar_one(),
        created_at=org.created_at,
        grace_period_ends_at=org.grace_period_ends_at,
        connections_total=connections_total_result.scalar_one(),
        connections_connected=connections_connected_result.scalar_one(),
    )


@router.post("/organizations/{organization_id}/subscription", response_model=AdminOrganizationDetailOut)
async def update_subscription(
    organization_id: str,
    payload: AdminSubscriptionUpdateRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminOrganizationDetailOut:
    """Manual override for support/billing troubleshooting -- unlike the
    tenant's own self-service switch_plan (app/api/v1/organizations.py),
    this deliberately bypasses the CANCELED-subscription block, since
    reactivating a canceled org is exactly the kind of thing this endpoint
    exists for. plan_id and subscription_status are independent, optional
    fields (at least one required, enforced in the schema) so a status-only
    reactivation doesn't force picking a plan in the same request, and vice
    versa.
    """
    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    before = {
        "plan_id": org.plan_id,
        "subscription_status": org.subscription_status.value,
        "plan_starts_at": org.plan_starts_at.isoformat() if org.plan_starts_at else None,
        "plan_expires_at": org.plan_expires_at.isoformat() if org.plan_expires_at else None,
    }

    if payload.plan_id is not None:
        plan = await db.get(Plan, payload.plan_id)
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")
        org.plan_id = plan.id
    if payload.subscription_status is not None:
        org.subscription_status = payload.subscription_status
    if payload.plan_starts_at is not None:
        org.plan_starts_at = payload.plan_starts_at
    if payload.plan_expires_at is not None:
        org.plan_expires_at = payload.plan_expires_at

    await record_audit_event(
        db,
        action="platform_admin.subscription_changed",
        target_type="organization",
        target_id=org.id,
        organization_id=org.id,
        actor_user_id=admin.id,
        before=before,
        after={
            "plan_id": org.plan_id,
            "subscription_status": org.subscription_status.value,
            "plan_starts_at": org.plan_starts_at.isoformat() if org.plan_starts_at else None,
            "plan_expires_at": org.plan_expires_at.isoformat() if org.plan_expires_at else None,
        },
        ip_address=_client_ip(request),
    )
    await db.commit()
    return await get_organization(organization_id, admin=admin, db=db)


def _to_admin_ticket_out(ticket: SupportTicket, org_name: str) -> AdminSupportTicketOut:
    return AdminSupportTicketOut(
        **SupportTicketOut.model_validate(ticket).model_dump(),
        organization_name=org_name,
        admin_unread=ticket.admin_unread,
    )


@router.get("/tickets", response_model=list[AdminSupportTicketOut])
async def list_tickets(
    status_filter: SupportTicketStatus | None = Query(default=None, alias="status"),
    organization_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AdminSupportTicketOut]:
    query = select(SupportTicket, Organization.name).join(
        Organization, Organization.id == SupportTicket.organization_id
    )
    if status_filter is not None:
        query = query.where(SupportTicket.status == status_filter)
    if organization_id is not None:
        query = query.where(SupportTicket.organization_id == organization_id)
    result = await db.execute(
        query.order_by(SupportTicket.updated_at.desc()).limit(limit).offset(offset)
    )
    return [_to_admin_ticket_out(ticket, org_name) for ticket, org_name in result.all()]


async def _get_ticket_or_404(db: AsyncSession, ticket_id: str) -> SupportTicket:
    ticket = await db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
    return ticket


@router.get("/tickets/{ticket_id}", response_model=AdminSupportTicketDetailOut)
async def get_ticket(
    ticket_id: str,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportTicketDetailOut:
    ticket = await _get_ticket_or_404(db, ticket_id)
    org = await db.get(Organization, ticket.organization_id)

    result = await db.execute(
        select(SupportTicketMessage)
        .where(SupportTicketMessage.ticket_id == ticket.id)
        .order_by(SupportTicketMessage.created_at.asc())
    )
    messages = list(result.scalars().all())

    # Viewing counts as acknowledging -- clears the admin-side "needs a
    # reply" signal even without posting a reply yet.
    ticket.admin_unread = False
    await db.commit()

    return AdminSupportTicketDetailOut(
        **SupportTicketOut.model_validate(ticket).model_dump(),
        organization_name=org.name if org else "",
        admin_unread=ticket.admin_unread,
        messages=[SupportTicketMessageOut.model_validate(m) for m in messages],
    )


@router.post(
    "/tickets/{ticket_id}/messages",
    response_model=SupportTicketMessageOut,
    status_code=status.HTTP_201_CREATED,
)
async def reply_to_ticket(
    ticket_id: str,
    payload: SupportTicketMessageCreateRequest,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> SupportTicketMessage:
    ticket = await _get_ticket_or_404(db, ticket_id)

    message = SupportTicketMessage(
        ticket_id=ticket.id,
        author_user_id=admin.id,
        author_is_admin=True,
        body=payload.body,
    )
    db.add(message)
    ticket.admin_unread = False

    await notify_user(
        db,
        user_id=ticket.created_by_user_id,
        organization_id=ticket.organization_id,
        type=NotificationType.SUPPORT_REPLY,
        title=f"Support replied: {ticket.subject}",
        body=payload.body[:500],
    )
    await record_audit_event(
        db,
        action="platform_admin.ticket_replied",
        target_type="support_ticket",
        target_id=ticket.id,
        organization_id=ticket.organization_id,
        actor_user_id=admin.id,
    )
    await db.commit()
    await db.refresh(message)
    return message


@router.post("/tickets/{ticket_id}/status", response_model=AdminSupportTicketOut)
async def set_ticket_status(
    ticket_id: str,
    payload: SupportTicketStatusUpdateRequest,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminSupportTicketOut:
    ticket = await _get_ticket_or_404(db, ticket_id)
    org = await db.get(Organization, ticket.organization_id)
    ticket.status = payload.status
    await db.commit()
    await db.refresh(ticket)
    return _to_admin_ticket_out(ticket, org.name if org else "")


@router.get("/audit-logs", response_model=list[AuditLogOut])
async def list_admin_audit_logs(
    organization_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AuditLog]:
    query = select(AuditLog)
    if organization_id is not None:
        query = query.where(AuditLog.organization_id == organization_id)
    result = await db.execute(query.order_by(AuditLog.created_at.desc()).limit(limit))
    return list(result.scalars().all())
