from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership, load_active_membership
from app.api.v1.schemas import ReconciliationItemOut, ReconciliationResolveRequest
from app.api.v1.sessions import broadcast_session_update
from app.db.models.contribution_event import ContributionDecision, ContributionEvent
from app.db.models.membership import Membership, Role
from app.db.models.reconciliation_item import (
    ReconciliationItem,
    ReconciliationResolution,
    ReconciliationStatus,
)
from app.db.models.session import Session
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.rbac import require_roles

router = APIRouter(tags=["reconciliation"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/v1/organizations/{organization_id}/reconciliation",
    response_model=list[ReconciliationItemOut],
)
async def list_reconciliation_items(
    organization_id: str,
    status_filter: ReconciliationStatus | None = None,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[ReconciliationItem]:
    # Read-only for any active member -- only resolve() below stays
    # Finance-only. Media initiates the sessions this queue is about; they
    # should be able to see its state too, not just Finance/Auditor/Owner.
    query = select(ReconciliationItem).where(ReconciliationItem.organization_id == organization_id)
    if status_filter is not None:
        query = query.where(ReconciliationItem.status == status_filter)
    result = await db.execute(query.order_by(ReconciliationItem.created_at.desc()))
    return list(result.scalars().all())


@router.post("/v1/reconciliation/{item_id}/resolve", response_model=ReconciliationItemOut)
async def resolve_reconciliation_item(
    item_id: str,
    payload: ReconciliationResolveRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ReconciliationItem:
    item = await db.get(ReconciliationItem, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reconciliation item not found."
        )

    # Reuses the standard org-membership dependency's tenant-isolation logic
    # by hand, since this route is keyed by item_id, not organization_id.
    membership = await load_active_membership(db, current_user.id, item.organization_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reconciliation item not found."
        )
    require_roles(membership, Role.FINANCE)

    if item.status != ReconciliationStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Item already resolved.")

    original_event = await db.get(ContributionEvent, item.contribution_event_id)
    if original_event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Original event not found.")

    now = datetime.now(timezone.utc)
    item.status = ReconciliationStatus.RESOLVED
    item.resolution = payload.resolution
    item.resolver_user_id = current_user.id
    item.resolved_at = now
    item.resolution_note = payload.note
    item.corrected_amount = payload.corrected_amount

    correction_event_id = None
    reversed_target_id = None
    if payload.resolution == ReconciliationResolution.ACCEPTED:
        amount = (
            payload.corrected_amount
            if payload.corrected_amount is not None
            else original_event.amount
        )
        if amount is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="corrected_amount is required: the original event has no extracted amount.",
            )
        # Never mutate the original event -- append a new, immutable ACCEPTED
        # entry that supersedes it for counting purposes (spec 7).
        correction = ContributionEvent(
            organization_id=original_event.organization_id,
            session_id=original_event.session_id,
            mailbox_connection_id=None,
            provider_message_id=f"correction-{item.id}",
            fingerprint=f"correction-{item.id}",
            received_at=original_event.received_at,
            decision=ContributionDecision.ACCEPTED,
            decision_reason=f"Finance-reviewed correction of event {original_event.id}.",
            amount=amount,
            currency=original_event.currency or "CAD",
            corrects_event_id=original_event.id,
            test_mode=original_event.test_mode,
        )
        db.add(correction)
        await db.flush()
        correction_event_id = correction.id
    elif payload.resolution == ReconciliationResolution.REVERSED:
        # This item's own event is a notification that an *earlier* accepted
        # deposit was returned/reversed (spec 7) -- Finance says which one.
        if not payload.reverses_event_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "reverses_event_id is required: which earlier accepted "
                    "contribution does this reverse?"
                ),
            )
        reversed_target = await db.get(ContributionEvent, payload.reverses_event_id)
        if reversed_target is None or reversed_target.session_id != item.session_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="reverses_event_id must reference a contribution event in this session.",
            )
        if reversed_target.decision != ContributionDecision.ACCEPTED:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only an accepted contribution can be reversed.",
            )
        if reversed_target.amount is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The contribution being reversed has no amount to reverse.",
            )
        existing_reversal = await db.execute(
            select(ContributionEvent).where(
                ContributionEvent.corrects_event_id == reversed_target.id,
                ContributionEvent.decision == ContributionDecision.REVERSED,
            )
        )
        if existing_reversal.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This contribution has already been reversed.",
            )

        # Never mutate the event being reversed -- append a new REVERSED
        # entry that nets it back out of the session's count/total
        # (app/domain/ledger.py), the same append-only pattern as an
        # accepted correction.
        correction = ContributionEvent(
            organization_id=original_event.organization_id,
            session_id=original_event.session_id,
            mailbox_connection_id=None,
            provider_message_id=f"reversal-{item.id}",
            fingerprint=f"reversal-{item.id}",
            received_at=original_event.received_at,
            decision=ContributionDecision.REVERSED,
            decision_reason=f"Finance-confirmed reversal of event {reversed_target.id}.",
            amount=reversed_target.amount,
            currency=reversed_target.currency or "CAD",
            corrects_event_id=reversed_target.id,
            test_mode=original_event.test_mode,
        )
        db.add(correction)
        await db.flush()
        correction_event_id = correction.id
        reversed_target_id = reversed_target.id

    await record_audit_event(
        db,
        action="reconciliation.resolved",
        target_type="reconciliation_item",
        target_id=item.id,
        organization_id=item.organization_id,
        actor_user_id=current_user.id,
        before={"status": "pending"},
        after={
            "resolution": payload.resolution.value,
            "corrected_amount": str(payload.corrected_amount) if payload.corrected_amount else None,
            "correction_event_id": correction_event_id,
            "reverses_event_id": reversed_target_id,
        },
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(item)

    if correction_event_id is not None:
        session = await db.get(Session, item.session_id)
        if session is not None:
            await broadcast_session_update(db, session)

    return item
