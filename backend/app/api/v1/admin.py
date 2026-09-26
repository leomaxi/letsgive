from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_platform_admin
from app.api.v1.schemas import (
    AdminAnnualSavingsUpdateRequest,
    AdminBonusSessionsRequest,
    AdminOrganizationDetailOut,
    AdminOrganizationOut,
    AdminPlanPricingUpdateRequest,
    AdminPromotionCreateRequest,
    AdminPromotionUpdateRequest,
    AdminRefundRequest,
    AdminSubscriptionUpdateRequest,
    AdminSupportTicketDetailOut,
    AdminSupportTicketOut,
    AuditLogOut,
    BillingPromotionOut,
    BillingRefundOut,
    BillingSettingsOut,
    PlanOut,
    SupportTicketMessageCreateRequest,
    SupportTicketMessageOut,
    SupportTicketOut,
    SupportTicketStatusUpdateRequest,
)
from app.db.models.audit_log import AuditLog
from app.db.models.billing import BillingPromotion, BillingSubscription
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.membership import Membership, MembershipStatus, Role
from app.db.models.notification import NotificationType
from app.db.models.organization import Organization
from app.db.models.plan import Plan
from app.db.models.support_ticket import SupportTicket, SupportTicketStatus
from app.db.models.support_ticket_message import SupportTicketMessage
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.inbox import notify_user
from app.domain.notifications import Notifier, get_notifier
from app.domain.subscriptions import create_prorated_refund
from app.domain.subscriptions import get_billing_settings as load_billing_settings
from app.integrations.paypal import PayPalClient, get_paypal_client

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _notify_owners_on_plan_price_change(
    db: AsyncSession,
    *,
    plan: Plan,
    old_price_cents: int,
    reason: str,
    notifier: Notifier,
) -> int:
    rows = await db.execute(
        select(Organization, User)
        .join(Membership, Membership.organization_id == Organization.id)
        .join(User, User.id == Membership.user_id)
        .where(
            Organization.plan_id == plan.id,
            Membership.role == Role.OWNER,
            Membership.status == MembershipStatus.ACTIVE,
        )
    )
    notified = 0
    old_price = old_price_cents / 100
    new_price = plan.monthly_price_cents / 100
    for org, owner in rows.all():
        body = (
            f"The monthly subscription price for {plan.name} changed from "
            f"USD {old_price:.2f} to USD {new_price:.2f}. Yearly billing keeps its "
            f"annual discount on top of the new price.\n\nReason: {reason}\n\n"
            "If you pay through PayPal, your current price stays in place until you "
            "approve the new pricing from the Billing page in Let's Give."
        )
        await notify_user(
            db,
            user_id=owner.id,
            organization_id=org.id,
            type=NotificationType.BILLING_NOTICE,
            title=f"{plan.name} subscription price changed",
            body=body[:1000],
        )
        await notifier.send_billing_notice(
            to_email=owner.email,
            subject=f"Let's Give: {plan.name} subscription price changed",
            body=body,
        )
        notified += 1
    return notified


async def _subscription_period_end(db: AsyncSession, org: Organization) -> datetime | None:
    result = await db.execute(
        select(BillingSubscription)
        .where(BillingSubscription.organization_id == org.id)
        .order_by(BillingSubscription.created_at.desc())
    )
    subscription = result.scalars().first()
    if subscription is not None and subscription.current_period_end is not None:
        return subscription.current_period_end
    return org.plan_expires_at


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
            discount_percent=org.discount_percent,
            bonus_sessions=org.bonus_sessions,
            bonus_sessions_expires_at=org.bonus_sessions_expires_at,
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
        discount_percent=org.discount_percent,
        bonus_sessions=org.bonus_sessions,
        bonus_sessions_expires_at=org.bonus_sessions_expires_at,
        member_count=member_count_result.scalar_one(),
        created_at=org.created_at,
        grace_period_ends_at=org.grace_period_ends_at,
        subscription_period_ends_at=await _subscription_period_end(db, org),
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
        "discount_percent": org.discount_percent,
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
    if payload.discount_percent is not None:
        org.discount_percent = payload.discount_percent

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
            "discount_percent": org.discount_percent,
        },
        ip_address=_client_ip(request),
    )
    await db.commit()
    return await get_organization(organization_id, admin=admin, db=db)


@router.post("/organizations/{organization_id}/bonus-sessions", response_model=AdminOrganizationDetailOut)
async def grant_bonus_sessions(
    organization_id: str,
    payload: AdminBonusSessionsRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminOrganizationDetailOut:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    period_end = await _subscription_period_end(db, org)
    if period_end is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bonus sessions require an existing subscription period end date.",
        )
    if payload.expires_at > period_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Bonus sessions must expire within the existing subscription period.",
        )

    before = {
        "bonus_sessions": org.bonus_sessions,
        "bonus_sessions_expires_at": org.bonus_sessions_expires_at.isoformat()
        if org.bonus_sessions_expires_at
        else None,
    }
    org.bonus_sessions = payload.bonus_sessions
    org.bonus_sessions_expires_at = payload.expires_at

    await record_audit_event(
        db,
        action="platform_admin.bonus_sessions_changed",
        target_type="organization",
        target_id=org.id,
        organization_id=org.id,
        actor_user_id=admin.id,
        before=before,
        after={
            "bonus_sessions": org.bonus_sessions,
            "bonus_sessions_expires_at": org.bonus_sessions_expires_at.isoformat(),
        },
        ip_address=_client_ip(request),
    )
    await db.commit()
    return await get_organization(organization_id, admin=admin, db=db)


@router.post("/plans/{plan_id}/pricing", response_model=PlanOut)
async def update_plan_pricing(
    plan_id: str,
    payload: AdminPlanPricingUpdateRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
) -> Plan:
    plan = await db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")

    before_price = plan.monthly_price_cents
    plan.monthly_price_cents = payload.monthly_price_cents
    notified = await _notify_owners_on_plan_price_change(
        db,
        plan=plan,
        old_price_cents=before_price,
        reason=payload.reason,
        notifier=notifier,
    )
    await record_audit_event(
        db,
        action="platform_admin.plan_price_changed",
        target_type="plan",
        target_id=plan.id,
        actor_user_id=admin.id,
        before={"monthly_price_cents": before_price},
        after={
            "monthly_price_cents": plan.monthly_price_cents,
            "reason": payload.reason,
            "owners_notified": notified,
        },
        ip_address=_client_ip(request),
    )
    await db.commit()
    await db.refresh(plan)
    return plan


@router.get("/promotions", response_model=list[BillingPromotionOut])
async def list_promotions(
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> list[BillingPromotion]:
    result = await db.execute(select(BillingPromotion).order_by(BillingPromotion.created_at.desc()))
    return list(result.scalars().all())


@router.post("/promotions", response_model=BillingPromotionOut, status_code=status.HTTP_201_CREATED)
async def create_promotion(
    payload: AdminPromotionCreateRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> BillingPromotion:
    if payload.plan_id is not None and await db.get(Plan, payload.plan_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")
    promotion = BillingPromotion(**payload.model_dump())
    db.add(promotion)
    await db.flush()
    await record_audit_event(
        db,
        action="platform_admin.promotion_created",
        target_type="billing_promotion",
        target_id=promotion.id,
        actor_user_id=admin.id,
        after=payload.model_dump(mode="json"),
        ip_address=_client_ip(request),
    )
    await db.commit()
    await db.refresh(promotion)
    return promotion


@router.patch("/promotions/{promotion_id}", response_model=BillingPromotionOut)
async def update_promotion(
    promotion_id: str,
    payload: AdminPromotionUpdateRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> BillingPromotion:
    promotion = await db.get(BillingPromotion, promotion_id)
    if promotion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Promotion not found.")
    before = {"is_active": promotion.is_active}
    promotion.is_active = payload.is_active
    await record_audit_event(
        db,
        action="platform_admin.promotion_updated",
        target_type="billing_promotion",
        target_id=promotion.id,
        actor_user_id=admin.id,
        before=before,
        after={"is_active": promotion.is_active},
        ip_address=_client_ip(request),
    )
    await db.commit()
    await db.refresh(promotion)
    return promotion


@router.get("/billing-settings", response_model=BillingSettingsOut)
async def get_billing_settings(
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> BillingSettingsOut:
    settings = await load_billing_settings(db)
    await db.commit()
    return BillingSettingsOut(annual_savings_percent=settings.annual_savings_percent)


@router.post("/billing-settings", response_model=BillingSettingsOut)
async def update_billing_settings(
    payload: AdminAnnualSavingsUpdateRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> BillingSettingsOut:
    settings = await load_billing_settings(db)
    before = settings.annual_savings_percent
    settings.annual_savings_percent = payload.annual_savings_percent
    await record_audit_event(
        db,
        action="platform_admin.annual_savings_changed",
        target_type="billing_settings",
        target_id=settings.id,
        actor_user_id=admin.id,
        before={"annual_savings_percent": before},
        after={"annual_savings_percent": settings.annual_savings_percent},
        ip_address=_client_ip(request),
    )
    await db.commit()
    return BillingSettingsOut(annual_savings_percent=settings.annual_savings_percent)


@router.post("/organizations/{organization_id}/refund", response_model=BillingRefundOut)
async def process_refund(
    organization_id: str,
    payload: AdminRefundRequest,
    request: Request,
    admin: User = Depends(get_platform_admin),
    db: AsyncSession = Depends(get_db),
    paypal: PayPalClient = Depends(get_paypal_client),
) -> BillingRefundOut:
    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    try:
        refund = await create_prorated_refund(
            db,
            organization_id=organization_id,
            requested_by_user_id=admin.id,
            reason=payload.reason,
            paypal=paypal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    await record_audit_event(
        db,
        action="platform_admin.refund_processed",
        target_type="billing_refund",
        target_id=refund.id,
        organization_id=organization_id,
        actor_user_id=admin.id,
        after={
            "amount": str(refund.amount),
            "currency": refund.currency,
            "reason": refund.reason,
            "paypal_refund_id": refund.provider_refund_id,
        },
        ip_address=_client_ip(request),
    )
    await db.commit()
    await db.refresh(refund)
    return BillingRefundOut.model_validate(refund)


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
