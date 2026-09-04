from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.api.v1.schemas import InvitationOut
from app.db.models.membership import Membership, MembershipStatus
from app.db.models.organization import Organization
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event

router = APIRouter(prefix="/v1/me", tags=["me"])


def _to_invitation_out(membership: Membership, org: Organization) -> InvitationOut:
    return InvitationOut(
        id=membership.id,
        organization_id=membership.organization_id,
        organization_name=org.name,
        role=membership.role,
        status=membership.status,
        created_at=membership.created_at,
    )


async def _load_invited_membership(
    db: AsyncSession, membership_id: str, user_id: str
) -> tuple[Membership, Organization]:
    result = await db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(
            Membership.id == membership_id,
            Membership.user_id == user_id,
            Membership.status == MembershipStatus.INVITED,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found.")
    return row


@router.get("/invitations", response_model=list[InvitationOut])
async def list_my_invitations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[InvitationOut]:
    result = await db.execute(
        select(Membership, Organization)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(Membership.user_id == current_user.id, Membership.status == MembershipStatus.INVITED)
        .order_by(Membership.created_at.desc())
    )
    return [_to_invitation_out(m, org) for m, org in result.all()]


@router.post("/invitations/{membership_id}/accept", response_model=InvitationOut)
async def accept_invitation(
    request: Request,
    membership_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InvitationOut:
    membership, org = await _load_invited_membership(db, membership_id, current_user.id)
    membership.status = MembershipStatus.ACTIVE

    await record_audit_event(
        db,
        action="membership.accepted",
        target_type="membership",
        target_id=membership.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        after={"role": membership.role.value},
        ip_address=request.client.host if request.client else None,
    )

    await db.commit()
    await db.refresh(membership)
    return _to_invitation_out(membership, org)


@router.post("/invitations/{membership_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline_invitation(
    request: Request,
    membership_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    membership, _org = await _load_invited_membership(db, membership_id, current_user.id)

    await record_audit_event(
        db,
        action="membership.declined",
        target_type="membership",
        target_id=membership.id,
        organization_id=membership.organization_id,
        actor_user_id=current_user.id,
        before={"role": membership.role.value},
        ip_address=request.client.host if request.client else None,
    )

    await db.delete(membership)
    await db.commit()
