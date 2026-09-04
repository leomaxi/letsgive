import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.v1.deps import (
    get_current_user,
    get_display_session,
    get_membership,
    get_session_membership,
    load_active_membership,
)
from app.api.v1.schemas import (
    ContributionEventOut,
    DisplayTemplateOut,
    DisplayTokenResponse,
    ExpectedVersionRequest,
    ExtendSessionRequest,
    OperatorSocketTokenResponse,
    RequestApprovalResponse,
    SessionCreateRequest,
    SessionOperatorOut,
    SessionPublicOut,
    SimulateDepositRequest,
    VerifyApprovalRequest,
    VisibilityRequest,
)
from app.core.security import (
    create_display_token,
    create_operator_socket_token,
    decode_display_token,
    decode_operator_socket_token,
    generate_otp_code,
    hash_otp_code,
    verify_otp_code,
)
from app.db.base import ensure_utc
from app.db.models.approval import APPROVAL_TTL_MINUTES, MAX_APPROVAL_ATTEMPTS, Approval
from app.db.models.contribution_event import ContributionDecision, ContributionEvent
from app.db.models.display_template import DisplayTemplate, ElementType
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
from app.db.models.membership import Membership, MembershipStatus, Role
from app.db.models.organization import Organization
from app.db.models.session import Session, SessionStatus
from app.db.models.user import User
from app.db.session import get_db
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_create_session
from app.domain.ledger import compute_ledger_totals
from app.domain.notifications import Notifier, get_notifier
from app.domain.qr import qr_data_uri
from app.domain.rbac import require_roles
from app.domain.realtime import get_broadcaster
from app.domain.sessions import check_version, compute_operator_warning, transition

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
# Separate router: list-by-org naturally nests under /v1/organizations/{id},
# not /v1/sessions, so it can't share `router`'s prefix with everything else
# below. Colocated in this module anyway since it needs the same
# ledger-totals/response-building helpers as the rest of the session routes.
org_sessions_router = APIRouter(prefix="/v1/organizations/{organization_id}", tags=["sessions"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _operator_response(db: AsyncSession, session: Session) -> SessionOperatorOut:
    count, total = await compute_ledger_totals(db, session.id)
    warning = await compute_operator_warning(db, session)
    out = SessionOperatorOut.model_validate(session)
    return out.model_copy(
        update={"contribution_count": count, "total_amount": total, "operator_warning": warning}
    )


async def _public_payload(db: AsyncSession, session: Session) -> SessionPublicOut:
    org = await db.get(Organization, session.organization_id)
    count, total = await compute_ledger_totals(db, session.id)
    return SessionPublicOut(
        status=session.status,
        organization_name=org.name if org else "",
        currency=session.currency,
        ends_at=session.ends_at,
        goal_amount=session.goal_amount if session.goal_enabled else None,
        amount_visible=session.amount_visible,
        contribution_count=count,
        total_amount=total if session.amount_visible else None,
    )


def _operator_topic(session_id: str) -> str:
    return f"{session_id}:operator"


async def broadcast_session_update(db: AsyncSession, session: Session) -> None:
    """Pushes the current state to every connected client for this session
    (spec 9: 'Realtime') -- both the sanitized public projection channel and
    the fuller operator channel (contribution_count/total_amount plus
    operator-only fields like version and operator_warning, used by the
    session detail page instead of polling). Safe to call liberally -- a
    no-op on either topic if nobody is subscribed to it.
    """
    public_payload = await _public_payload(db, session)
    await get_broadcaster().publish(session.id, public_payload.model_dump(mode="json"))

    operator_payload = await _operator_response(db, session)
    await get_broadcaster().publish(
        _operator_topic(session.id), operator_payload.model_dump(mode="json")
    )


@router.post("", response_model=SessionOperatorOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    membership = await load_active_membership(db, current_user.id, payload.organization_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    require_roles(membership, Role.MEDIA, Role.FINANCE)

    org = await db.get(Organization, payload.organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_create_session(db, org)

    mailbox_connection_id = payload.mailbox_connection_id
    if mailbox_connection_id is not None:
        connection = await db.get(MailboxConnection, mailbox_connection_id)
        if (
            connection is None
            or connection.organization_id != org.id
            or connection.status != ConnectionStatus.CONNECTED
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="mailbox_connection_id must reference a connected mailbox in this organization.",
            )
    else:
        result = await db.execute(
            select(MailboxConnection).where(
                MailboxConnection.organization_id == org.id,
                MailboxConnection.status == ConnectionStatus.CONNECTED,
            )
        )
        connected = list(result.scalars().all())
        if len(connected) == 1:
            mailbox_connection_id = connected[0].id

    display_template_id = payload.display_template_id
    if display_template_id is not None:
        template = await db.get(DisplayTemplate, display_template_id)
        if template is None or template.organization_id != org.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="display_template_id must reference a template in this organization.",
            )
    else:
        result = await db.execute(
            select(DisplayTemplate).where(
                DisplayTemplate.organization_id == org.id, DisplayTemplate.is_default.is_(True)
            )
        )
        default_template = result.scalars().first()
        if default_template is not None:
            display_template_id = default_template.id

    session = Session(
        organization_id=org.id,
        created_by_user_id=current_user.id,
        mailbox_connection_id=mailbox_connection_id,
        status=SessionStatus.DRAFT,
        contribution_method=payload.contribution_method,
        currency=org.currency,
        duration_seconds=payload.duration_seconds,
        display_template_id=display_template_id,
        goal_enabled=payload.goal_enabled,
        goal_amount=payload.goal_amount,
        test_mode=payload.test_mode,
    )
    db.add(session)
    await db.flush()

    await record_audit_event(
        db,
        action="session.created",
        target_type="session",
        target_id=session.id,
        organization_id=org.id,
        actor_user_id=current_user.id,
        after={"contribution_method": session.contribution_method, "test_mode": session.test_mode},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    return await _operator_response(db, session)


@org_sessions_router.get("/sessions", response_model=list[SessionOperatorOut])
async def list_org_sessions(
    organization_id: str,
    status_filter: SessionStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, le=200),
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[SessionOperatorOut]:
    query = select(Session).where(Session.organization_id == organization_id)
    if status_filter is not None:
        query = query.where(Session.status == status_filter)
    query = query.order_by(Session.created_at.desc()).limit(limit)

    result = await db.execute(query)
    sessions = list(result.scalars().all())
    return [await _operator_response(db, session) for session in sessions]


@router.post("/{session_id}/request-approval", response_model=RequestApprovalResponse)
async def request_approval(
    session_id: str,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
) -> RequestApprovalResponse:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA)

    if session.status not in (SessionStatus.DRAFT, SessionStatus.APPROVAL_REQUESTED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot request approval from status '{session.status.value}'.",
        )

    result = await db.execute(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.organization_id == session.organization_id,
            Membership.role == Role.FINANCE,
            Membership.status == MembershipStatus.ACTIVE,
        )
    )
    finance_users = list(result.scalars().all())
    if not finance_users:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This organization has no active finance officer to approve sessions.",
        )

    code = generate_otp_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=APPROVAL_TTL_MINUTES)
    approval = Approval(
        session_id=session.id,
        requested_by_user_id=current_user.id,
        approver_user_id=finance_users[0].id if len(finance_users) == 1 else None,
        code_hash=hash_otp_code(code),
        expires_at=expires_at,
    )
    db.add(approval)

    transition(session, SessionStatus.APPROVAL_REQUESTED)

    await db.flush()

    for finance_user in finance_users:
        await notifier.send_otp(to_email=finance_user.email, code=code, session_id=session.id)

    await record_audit_event(
        db,
        action="session.approval_requested",
        target_type="approval",
        target_id=approval.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"expires_at": expires_at.isoformat(), "notified": len(finance_users)},
        ip_address=_client_ip(request),
    )

    await db.commit()
    return RequestApprovalResponse(
        approval_id=approval.id,
        expires_at=expires_at,
        sent_to=[u.email for u in finance_users],
    )


@router.post("/{session_id}/verify", response_model=SessionOperatorOut)
async def verify_approval(
    session_id: str,
    payload: VerifyApprovalRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA)

    approval = await db.get(Approval, payload.approval_id)
    if approval is None or approval.session_id != session.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval not found.")

    if approval.verified_at is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code_already_used")
    if approval.locked:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code_locked")
    if ensure_utc(approval.expires_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="code_expired")

    if not verify_otp_code(payload.code, approval.code_hash):
        approval.attempts += 1
        if approval.attempts >= MAX_APPROVAL_ATTEMPTS:
            approval.locked = True
        await record_audit_event(
            db,
            action="session.verify_failed",
            target_type="approval",
            target_id=approval.id,
            organization_id=session.organization_id,
            actor_user_id=current_user.id,
            after={"attempts": approval.attempts, "locked": approval.locked},
            ip_address=_client_ip(request),
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code_locked" if approval.locked else "code_invalid",
        )

    now = datetime.now(timezone.utc)
    approval.verified_at = now
    session.watermark = now
    transition(session, SessionStatus.AUTHORIZED)

    await record_audit_event(
        db,
        action="session.authorized",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"watermark": now.isoformat(), "approval_id": approval.id},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.post("/{session_id}/start", response_model=SessionOperatorOut)
async def start_session(
    session_id: str,
    payload: ExpectedVersionRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    check_version(session, payload.expected_version)

    now = datetime.now(timezone.utc)
    session.starts_at = now
    session.ends_at = now + timedelta(seconds=session.duration_seconds)
    transition(session, SessionStatus.LIVE)

    await record_audit_event(
        db,
        action="session.started",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"starts_at": now.isoformat(), "ends_at": session.ends_at.isoformat()},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.post("/{session_id}/pause", response_model=SessionOperatorOut)
async def pause_session(
    session_id: str,
    payload: ExpectedVersionRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    check_version(session, payload.expected_version)

    session.paused_at = datetime.now(timezone.utc)
    transition(session, SessionStatus.PAUSED)

    await record_audit_event(
        db,
        action="session.paused",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.post("/{session_id}/resume", response_model=SessionOperatorOut)
async def resume_session(
    session_id: str,
    payload: ExpectedVersionRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    check_version(session, payload.expected_version)

    if session.paused_at is not None and session.ends_at is not None:
        paused_duration = datetime.now(timezone.utc) - ensure_utc(session.paused_at)
        session.ends_at = ensure_utc(session.ends_at) + paused_duration
    session.paused_at = None
    transition(session, SessionStatus.LIVE)

    await record_audit_event(
        db,
        action="session.resumed",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"ends_at": session.ends_at.isoformat() if session.ends_at else None},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.post("/{session_id}/extend", response_model=SessionOperatorOut)
async def extend_session(
    session_id: str,
    payload: ExtendSessionRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    check_version(session, payload.expected_version)

    if session.status not in (SessionStatus.LIVE, SessionStatus.PAUSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only a live or paused session can be extended.",
        )

    if session.ends_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Session has no end time set.")
    session.ends_at = ensure_utc(session.ends_at) + timedelta(seconds=payload.additional_seconds)
    session.version += 1

    await record_audit_event(
        db,
        action="session.extended",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"additional_seconds": payload.additional_seconds, "ends_at": session.ends_at.isoformat()},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.patch("/{session_id}/visibility", response_model=SessionOperatorOut)
async def set_visibility(
    session_id: str,
    payload: VisibilityRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.FINANCE)
    check_version(session, payload.expected_version)

    before = session.amount_visible
    session.amount_visible = payload.amount_visible
    session.version += 1

    await record_audit_event(
        db,
        action="session.visibility_changed",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        before={"amount_visible": before},
        after={"amount_visible": session.amount_visible},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.post("/{session_id}/close", response_model=SessionOperatorOut)
async def close_session(
    session_id: str,
    payload: ExpectedVersionRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    check_version(session, payload.expected_version)

    now = datetime.now(timezone.utc)
    if session.ends_at is None or now < ensure_utc(session.ends_at):
        session.ends_at = now
    transition(session, SessionStatus.ENDED)

    await record_audit_event(
        db,
        action="session.closed",
        target_type="session",
        target_id=session.id,
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"ends_at": session.ends_at.isoformat()},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(session)
    await broadcast_session_update(db, session)
    return await _operator_response(db, session)


@router.get("/{session_id}/operator", response_model=SessionOperatorOut)
async def get_operator_state(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    db: AsyncSession = Depends(get_db),
) -> SessionOperatorOut:
    # Read-only for any active member (Owner/Auditor included), matching
    # reports/reconciliation -- only the control actions below (start,
    # pause, etc.) stay Media/Finance-only. Without this, an Owner or
    # Auditor clicking through from the sessions list (which links every
    # role to this page) would 403 and see a confusing "not found".
    session, _membership = session_membership
    return await _operator_response(db, session)


@router.post("/{session_id}/display-token", response_model=DisplayTokenResponse)
async def issue_display_token(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
) -> DisplayTokenResponse:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    expire_minutes = 240
    return DisplayTokenResponse(
        display_token=create_display_token(session.id, expire_minutes=expire_minutes),
        expires_in_minutes=expire_minutes,
    )


@router.post("/{session_id}/operator-socket-token", response_model=OperatorSocketTokenResponse)
async def issue_operator_socket_token(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
) -> OperatorSocketTokenResponse:
    """Short-lived credential for the operator console's realtime channel
    (WS /{session_id}/live-operator) -- minted over a normal authenticated
    REST call so the general-purpose access token never has to go in a URL.
    """
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)
    expire_minutes = 60
    return OperatorSocketTokenResponse(
        operator_socket_token=create_operator_socket_token(
            session.id, current_user.id, expire_minutes=expire_minutes
        ),
        expires_in_minutes=expire_minutes,
    )


@router.get("/{session_id}/public", response_model=SessionPublicOut)
async def get_public_state(
    session: Session = Depends(get_display_session),
    db: AsyncSession = Depends(get_db),
) -> SessionPublicOut:
    return await _public_payload(db, session)


@router.get("/{session_id}/display-template/public", response_model=DisplayTemplateOut | None)
async def get_public_display_template(
    session: Session = Depends(get_display_session),
    db: AsyncSession = Depends(get_db),
) -> DisplayTemplateOut | None:
    """The layout the projection page renders (spec 6.1/6.2), fetched with
    the same display token as the WebSocket channel -- no org membership
    check, since the token itself is the credential and the response
    carries no donor data, just positions/styles/static text.
    """
    if session.display_template_id is None:
        return None
    result = await db.execute(
        select(DisplayTemplate)
        .options(selectinload(DisplayTemplate.elements))
        .where(DisplayTemplate.id == session.display_template_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        return None

    out = DisplayTemplateOut.model_validate(template)
    for element in out.elements:
        # QR images are rendered here, not stored: only the raw value is
        # persisted, so re-rendering (e.g. after the value changes) never
        # needs a migration or a stale cached image to invalidate.
        if element.type == ElementType.QR_CODE:
            value = element.binding.get("value")
            if value:
                element.binding = {**element.binding, "qr_data_uri": qr_data_uri(str(value))}
    return out


@router.post(
    "/{session_id}/simulate-deposit",
    response_model=ContributionEventOut,
    status_code=status.HTTP_201_CREATED,
)
async def simulate_deposit(
    session_id: str,
    payload: SimulateDepositRequest,
    request: Request,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContributionEvent:
    session, membership = session_membership
    require_roles(membership, Role.MEDIA, Role.FINANCE)

    if not session.test_mode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Synthetic deposits can only be simulated for a test_mode session.",
        )
    if session.status not in (SessionStatus.LIVE, SessionStatus.PAUSED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session must be live or paused to simulate a deposit.",
        )

    event = ContributionEvent(
        organization_id=session.organization_id,
        session_id=session.id,
        # Synthetic events aren't tied to a real mailbox connection at all.
        mailbox_connection_id=None,
        provider_message_id=f"synthetic-{uuid.uuid4()}",
        fingerprint=f"synthetic-{uuid.uuid4()}",
        received_at=datetime.now(timezone.utc),
        decision=ContributionDecision.ACCEPTED,
        decision_reason="Synthetic deposit for display rehearsal (test_mode).",
        amount=payload.amount,
        currency=session.currency,
        test_mode=True,
    )
    db.add(event)

    await record_audit_event(
        db,
        action="session.simulated_deposit",
        target_type="contribution_event",
        organization_id=session.organization_id,
        actor_user_id=current_user.id,
        after={"amount": str(payload.amount), "session_id": session.id},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(event)
    await broadcast_session_update(db, session)
    return event


@router.get("/{session_id}/events", response_model=list[ContributionEventOut])
async def list_session_events(
    session_id: str,
    session_membership: tuple[Session, Membership] = Depends(get_session_membership),
    db: AsyncSession = Depends(get_db),
) -> list[ContributionEvent]:
    # Read-only for any active member -- same reasoning as GET .../operator
    # above: the session detail page any role can reach also renders this
    # ledger list, and an Owner/Auditor can already see the same events via
    # the session report (reports.py), so there's nothing gained by 403ing
    # them here.
    session, _membership = session_membership

    result = await db.execute(
        select(ContributionEvent)
        .where(ContributionEvent.session_id == session.id)
        .order_by(ContributionEvent.received_at.desc())
    )
    return list(result.scalars().all())


@router.websocket("/{session_id}/live")
async def session_live(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Public realtime channel (spec 6.2 / 9): pushes sanitized session state
    to a projection/OBS client as it changes, instead of the client polling
    GET .../public. Auth is a display token in the query string -- browsers
    can't attach custom headers to a WebSocket handshake, and a display
    token is short-lived and carries no finance permissions, so it's safe
    to put in a URL the same way GET .../public's Authorization header
    would carry it.
    """
    if token is None:
        await websocket.close(code=4401)
        return
    try:
        token_session_id = decode_display_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    if token_session_id != session_id:
        await websocket.close(code=4401)
        return

    session = await db.get(Session, session_id)
    if session is None:
        await websocket.close(code=4404)
        return
    initial_payload = await _public_payload(db, session)
    # Release the DB connection now rather than holding it for the whole
    # (potentially long-lived) socket -- everything after this point is
    # served from the in-process broadcaster, no further queries needed.
    await db.close()

    await websocket.accept()
    queue = get_broadcaster().subscribe(session_id)
    try:
        await websocket.send_json(initial_payload.model_dump(mode="json"))
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        get_broadcaster().unsubscribe(session_id, queue)


@router.websocket("/{session_id}/live-operator")
async def session_live_operator(
    websocket: WebSocket,
    session_id: str,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Realtime channel for the operator console (session detail page) --
    same idea as /live, but carries the fuller operator payload (version,
    operator_warning, etc.) and requires proof of active Media/Finance
    membership rather than a display token. Auth is an operator socket
    token in the query string (see create_operator_socket_token for why).
    """
    if token is None:
        await websocket.close(code=4401)
        return
    try:
        token_session_id, user_id = decode_operator_socket_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=4401)
        return
    if token_session_id != session_id:
        await websocket.close(code=4401)
        return

    session = await db.get(Session, session_id)
    if session is None:
        await websocket.close(code=4404)
        return

    # Re-verify membership at connect time rather than trusting the token's
    # age alone -- someone removed from the org after the token was minted
    # shouldn't keep a live operator feed for up to its full TTL.
    membership = await load_active_membership(db, user_id, session.organization_id)
    if membership is None or membership.role not in (Role.MEDIA, Role.FINANCE):
        await websocket.close(code=4403)
        return

    initial_payload = await _operator_response(db, session)
    await db.close()

    await websocket.accept()
    topic = _operator_topic(session_id)
    queue = get_broadcaster().subscribe(topic)
    try:
        await websocket.send_json(initial_payload.model_dump(mode="json"))
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except WebSocketDisconnect:
        pass
    finally:
        get_broadcaster().unsubscribe(topic, queue)
