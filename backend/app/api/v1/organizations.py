from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    MemberInviteRequest,
    MemberRoleUpdateRequest,
    MembershipOut,
    MyOrganizationOut,
    OrganizationCreateRequest,
    OrganizationOut,
    SwitchPlanRequest,
)
from app.db.models.membership import MFA_REQUIRED_ROLES, Membership, MembershipStatus, Role
from app.db.models.organization import Organization, SubscriptionStatus
from app.db.models.plan import Plan
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_add_team_member
from app.domain.rbac import require_roles

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])


def _to_membership_out(membership: Membership, user: User) -> MembershipOut:
    return MembershipOut(
        id=membership.id,
        user_id=membership.user_id,
        user_email=user.email,
        user_full_name=user.full_name,
        organization_id=membership.organization_id,
        role=membership.role,
        status=membership.status,
        created_at=membership.created_at,
    )


@router.post("", response_model=OrganizationOut, status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    if not current_user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MFA must be enabled before creating an organization (you become its Owner).",
        )

    result = await db.execute(select(Plan).where(Plan.key == "starter"))
    starter_plan = result.scalar_one_or_none()

    org = Organization(
        name=payload.name,
        legal_name=payload.legal_name,
        country=payload.country.upper(),
        timezone=payload.timezone,
        currency=payload.currency.upper(),
        nonprofit_type=",".join(payload.nonprofit_type) if payload.nonprofit_type else None,
        plan_id=starter_plan.id if starter_plan else None,
        subscription_status=SubscriptionStatus.TRIALING,
    )
    db.add(org)
    await db.flush()

    owner_membership = Membership(
        user_id=current_user.id,
        organization_id=org.id,
        role=Role.OWNER,
        status=MembershipStatus.ACTIVE,
    )
    db.add(owner_membership)

    await record_audit_event(
        db,
        action="organization.created",
        target_type="organization",
        target_id=org.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={"name": org.name, "country": org.country, "currency": org.currency},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(org)
    return org


@router.get("", response_model=list[MyOrganizationOut])
async def list_my_organizations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[MyOrganizationOut]:
    """Every org the current user is an active member of, with their role in
    each -- what a client needs to build an org switcher on login without a
    request per organization. Only ACTIVE memberships; pending ones live at
    GET /v1/me/invitations until accepted or declined.
    """
    result = await db.execute(
        select(Organization, Membership.role, Membership.status)
        .join(Membership, Membership.organization_id == Organization.id)
        .where(Membership.user_id == current_user.id, Membership.status == MembershipStatus.ACTIVE)
        .order_by(Organization.name)
    )
    return [
        MyOrganizationOut(
            **OrganizationOut.model_validate(org).model_dump(),
            role=role,
            membership_status=membership_status,
        )
        for org, role, membership_status in result.all()
    ]


@router.get("/{organization_id}", response_model=OrganizationOut)
async def get_organization(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    return org


@router.get("/{organization_id}/members", response_model=list[MembershipOut])
async def list_members(
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[MembershipOut]:
    result = await db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.organization_id == membership.organization_id,
            # REQUESTED rows have no role yet and live in their own
            # "join requests" list (GET .../join-requests) instead --
            # mixing them in here would break MembershipOut.role (non-null)
            # and confuse "member" with "asked to join".
            Membership.status != MembershipStatus.REQUESTED,
        )
        .order_by(Membership.created_at)
    )
    return [_to_membership_out(m, u) for m, u in result.all()]


@router.post(
    "/{organization_id}/members/invite",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    payload: MemberInviteRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MembershipOut:
    require_roles(membership, Role.OWNER)

    result = await db.execute(select(User).where(User.email == payload.email))
    invitee = result.scalar_one_or_none()
    if invitee is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No registered user with that email. They must register before being invited.",
        )

    if payload.role in MFA_REQUIRED_ROLES and not invitee.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The '{payload.role.value}' role requires the invitee to have MFA enabled.",
        )

    existing = await db.execute(
        select(Membership).where(
            Membership.user_id == invitee.id,
            Membership.organization_id == membership.organization_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User is already a member.")

    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_add_team_member(db, org)

    new_membership = Membership(
        user_id=invitee.id,
        organization_id=membership.organization_id,
        role=payload.role,
        status=MembershipStatus.INVITED,
    )
    db.add(new_membership)
    await db.flush()

    await record_audit_event(
        db,
        action="membership.invited",
        target_type="membership",
        target_id=new_membership.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        after={"invitee_email": payload.email, "role": payload.role.value},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(new_membership)
    return _to_membership_out(new_membership, invitee)


@router.patch("/{organization_id}/members/{member_id}", response_model=MembershipOut)
async def update_member_role(
    member_id: str,
    payload: MemberRoleUpdateRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MembershipOut:
    require_roles(membership, Role.OWNER)

    result = await db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.id == member_id,
            Membership.organization_id == membership.organization_id,
            Membership.status != MembershipStatus.REQUESTED,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found.")
    target_membership, target_user = row

    if payload.role in MFA_REQUIRED_ROLES and not target_user.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The '{payload.role.value}' role requires the member to have MFA enabled.",
        )

    if target_membership.role == Role.OWNER and payload.role != Role.OWNER:
        owner_count_result = await db.execute(
            select(func.count(Membership.id)).where(
                Membership.organization_id == membership.organization_id,
                Membership.role == Role.OWNER,
                Membership.status == MembershipStatus.ACTIVE,
            )
        )
        if owner_count_result.scalar_one() <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This organization must keep at least one active owner.",
            )

    before_role = target_membership.role
    target_membership.role = payload.role

    await record_audit_event(
        db,
        action="membership.role_changed",
        target_type="membership",
        target_id=target_membership.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        before={"role": before_role.value if before_role else None},
        after={"role": payload.role.value, "member_email": target_user.email},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(target_membership)
    return _to_membership_out(target_membership, target_user)


@router.post("/{organization_id}/subscription/cancel", response_model=OrganizationOut)
async def cancel_subscription(
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    """Cancels immediately; new sessions/connections/templates stop right
    away, but historical reports stay readable through grace_period_ends_at
    (spec 13: "a reasonable read-only grace period after cancellation").
    """
    require_roles(membership, Role.OWNER)

    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    org.subscription_status = SubscriptionStatus.CANCELED
    org.grace_period_ends_at = datetime.now(timezone.utc) + timedelta(days=30)

    await record_audit_event(
        db,
        action="subscription.canceled",
        target_type="organization",
        target_id=org.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={"grace_period_ends_at": org.grace_period_ends_at.isoformat()},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(org)
    return org


@router.post("/{organization_id}/subscription/switch-plan", response_model=OrganizationOut)
async def switch_plan(
    payload: SwitchPlanRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Organization:
    """Changes plan immediately, in either direction. A downgrade doesn't
    retroactively touch anything the org already has (sessions, connections,
    templates over the new plan's limits keep working); the new limits just
    apply going forward, the same way entitlement checks already work at
    creation time (see app/domain/billing.py).
    """
    require_roles(membership, Role.OWNER)

    org = await db.get(Organization, membership.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    if org.subscription_status == SubscriptionStatus.CANCELED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This subscription is canceled. Contact support to reactivate it first.",
        )

    new_plan = await db.get(Plan, payload.plan_id)
    if new_plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found.")

    if new_plan.id == org.plan_id:
        return org

    previous_plan_id = org.plan_id
    org.plan_id = new_plan.id

    await record_audit_event(
        db,
        action="subscription.plan_changed",
        target_type="organization",
        target_id=org.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        before={"plan_id": previous_plan_id},
        after={"plan_id": new_plan.id, "plan_key": new_plan.key},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(org)
    return org
