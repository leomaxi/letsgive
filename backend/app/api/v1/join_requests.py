from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    ApproveJoinRequestRequest,
    JoinOrganizationRequest,
    JoinRequestOut,
    MembershipOut,
    OrgJoinRequestOut,
)
from app.db.models.membership import MFA_REQUIRED_ROLES, Membership, MembershipStatus, Role
from app.db.models.organization import Organization
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_add_team_member
from app.domain.rbac import require_roles

# Two routers, same pattern as sessions.py's router/org_sessions_router:
# "join" and the Owner-facing review endpoints are organization-scoped,
# while a requester's own view of their pending requests lives under /v1/me.
router = APIRouter(prefix="/v1/organizations", tags=["join-requests"])
me_router = APIRouter(prefix="/v1/me/join-requests", tags=["join-requests"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _to_join_request_out(membership: Membership, org: Organization) -> JoinRequestOut:
    return JoinRequestOut(
        id=membership.id,
        organization_id=membership.organization_id,
        organization_name=org.name,
        status=membership.status,
        created_at=membership.created_at,
    )


def _to_org_join_request_out(membership: Membership, user: User) -> OrgJoinRequestOut:
    return OrgJoinRequestOut(
        id=membership.id,
        organization_id=membership.organization_id,
        user_id=user.id,
        user_email=user.email,
        user_full_name=user.full_name,
        created_at=membership.created_at,
    )


@router.post("/join", response_model=JoinRequestOut, status_code=status.HTTP_201_CREATED)
async def join_organization(
    payload: JoinOrganizationRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JoinRequestOut:
    """Self-service alternative to an Owner-sent invite (spec follow-up): a
    user enters an organization's shareable code instead of waiting for an
    invite email. This only *requests* membership -- no role, no access --
    the Owner still has to approve it and choose a role (see approve_join_request
    below). Never lets the requester in on their own, matching invite_member's
    existing "MFA-required-role" and "already a member" guards conceptually,
    just deferred to approval time since no role is chosen yet.
    """
    result = await db.execute(
        select(Organization).where(Organization.join_code == payload.join_code.strip().upper())
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid organization code.")

    existing = await db.execute(
        select(Membership).where(
            Membership.user_id == current_user.id, Membership.organization_id == org.id
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have a membership or a pending request for this organization.",
        )

    membership = Membership(
        user_id=current_user.id,
        organization_id=org.id,
        role=None,
        status=MembershipStatus.REQUESTED,
    )
    db.add(membership)
    await db.flush()

    await record_audit_event(
        db,
        action="membership.join_requested",
        target_type="membership",
        target_id=membership.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={"user_email": current_user.email},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(membership)
    return _to_join_request_out(membership, org)


@router.get("/{organization_id}/join-requests", response_model=list[OrgJoinRequestOut])
async def list_join_requests(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[OrgJoinRequestOut]:
    require_roles(membership, Role.OWNER)
    result = await db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.organization_id == organization_id,
            Membership.status == MembershipStatus.REQUESTED,
        )
        .order_by(Membership.created_at)
    )
    return [_to_org_join_request_out(m, u) for m, u in result.all()]


async def _load_join_request(db: AsyncSession, organization_id: str, membership_id: str) -> Membership:
    result = await db.execute(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.organization_id == organization_id,
            Membership.status == MembershipStatus.REQUESTED,
        )
    )
    join_request = result.scalar_one_or_none()
    if join_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Join request not found.")
    return join_request


@router.post("/{organization_id}/join-requests/{membership_id}/approve", response_model=MembershipOut)
async def approve_join_request(
    organization_id: str,
    membership_id: str,
    payload: ApproveJoinRequestRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MembershipOut:
    require_roles(membership, Role.OWNER)

    join_request = await _load_join_request(db, organization_id, membership_id)
    requester = await db.get(User, join_request.user_id)
    if requester is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    if payload.role in MFA_REQUIRED_ROLES and not requester.mfa_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The '{payload.role.value}' role requires this user to have MFA enabled first.",
        )

    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_add_team_member(db, org)

    join_request.role = payload.role
    join_request.status = MembershipStatus.ACTIVE

    await record_audit_event(
        db,
        action="membership.join_approved",
        target_type="membership",
        target_id=join_request.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={"user_email": requester.email, "role": payload.role.value},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(join_request)
    return MembershipOut(
        id=join_request.id,
        user_id=join_request.user_id,
        user_email=requester.email,
        user_full_name=requester.full_name,
        organization_id=join_request.organization_id,
        role=join_request.role,
        status=join_request.status,
        created_at=join_request.created_at,
    )


@router.post(
    "/{organization_id}/join-requests/{membership_id}/deny", status_code=status.HTTP_204_NO_CONTENT
)
async def deny_join_request(
    organization_id: str,
    membership_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    require_roles(membership, Role.OWNER)

    join_request = await _load_join_request(db, organization_id, membership_id)

    await record_audit_event(
        db,
        action="membership.join_denied",
        target_type="membership",
        target_id=join_request.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        ip_address=_client_ip(request),
    )

    await db.delete(join_request)
    await db.commit()


@me_router.get("", response_model=list[JoinRequestOut])
async def list_my_join_requests(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[JoinRequestOut]:
    result = await db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Membership.user_id == current_user.id, Membership.status == MembershipStatus.REQUESTED)
        .order_by(Membership.created_at.desc())
    )
    return [_to_join_request_out(m, org) for m, org in result.all()]


@me_router.post("/{membership_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_my_join_request(
    membership_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.user_id == current_user.id,
            Membership.status == MembershipStatus.REQUESTED,
        )
    )
    join_request = result.scalar_one_or_none()
    if join_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Join request not found.")
    await db.delete(join_request)
    await db.commit()
