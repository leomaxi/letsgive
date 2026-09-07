from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    ImapCheckNowResponse,
    MailboxConnectionCreateRequest,
    MailboxConnectionOut,
    ParserProfileCreateRequest,
    ParserProfileOut,
    ParserProfileUpdateRequest,
    RecentImapMessageOut,
)
from app.db.models.contribution_event import ContributionEvent
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection, MailboxProviderName
from app.db.models.membership import Membership, Role
from app.db.models.organization import Organization
from app.db.models.parser_profile import (
    DEFAULT_AMOUNT_PATTERN,
    DEFAULT_CREDIT_KEYWORDS,
    DEFAULT_REJECT_KEYWORDS,
    ParserProfile,
)
from app.db.models.user import User
from app.db.session import get_db
from app.domain import imap_idle
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_create_connection
from app.domain.imap_polling import get_recent_messages, poll_imap_connection
from app.domain.imap_provider import ImapAuthError, connect_and_get_baseline_uid, guess_imap_host
from app.domain.mailbox_providers import get_provider
from app.domain.rbac import require_roles

router = APIRouter(prefix="/v1/organizations/{organization_id}", tags=["connections"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post(
    "/connections", response_model=MailboxConnectionOut, status_code=status.HTTP_201_CREATED
)
async def create_connection(
    organization_id: str,
    payload: MailboxConnectionCreateRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MailboxConnection:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    org = await db.get(Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found.")
    await assert_can_create_connection(db, org)

    connection = MailboxConnection(
        organization_id=organization_id,
        provider=payload.provider,
        mailbox=payload.mailbox,
        folder=payload.folder,
        status=ConnectionStatus.PENDING,
    )

    if payload.provider == MailboxProviderName.IMAP:
        # Pull-based, not OAuth -- "log in with your email" doesn't fit the
        # begin_authorization() redirect-style protocol the other providers
        # use, so it's handled directly here instead of through get_provider().
        if not payload.imap_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An app password is required to connect this mailbox.",
            )
        host, port = payload.imap_host, payload.imap_port
        if not host or not port:
            guessed = guess_imap_host(payload.mailbox)
            if guessed is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Couldn't recognize this email provider automatically. Please provide "
                        "its IMAP server address and port."
                    ),
                )
            host, port = host or guessed[0], port or guessed[1]

        try:
            baseline_uid = await connect_and_get_baseline_uid(
                host=host, port=port, mailbox=payload.mailbox, password=payload.imap_password
            )
        except ImapAuthError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        connection.imap_host = host
        connection.imap_port = port
        connection.imap_password = payload.imap_password
        connection.imap_last_uid = baseline_uid
        connection.status = ConnectionStatus.CONNECTED
        db.add(connection)
        await db.flush()
    else:
        db.add(connection)
        await db.flush()

        provider = get_provider(payload.provider)
        result = await provider.begin_authorization(
            organization_id=organization_id, mailbox=payload.mailbox, folder=payload.folder
        )
        if result.status == "connected":
            connection.status = ConnectionStatus.CONNECTED
            connection.external_account_id = result.external_account_id
            connection.token_ref = result.token_ref

    await record_audit_event(
        db,
        action="connection.created",
        target_type="mailbox_connection",
        target_id=connection.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={
            "provider": payload.provider.value,
            "mailbox": payload.mailbox,
            "status": connection.status.value,
        },
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(connection)

    # Started only after commit -- the watcher opens its own separate DB
    # session (app/domain/imap_idle.py) and would find nothing yet if
    # started while this request's transaction was still open.
    if connection.provider == MailboxProviderName.IMAP and connection.status == ConnectionStatus.CONNECTED:
        imap_idle.start_watching(connection.id)

    return connection


@router.get("/connections", response_model=list[MailboxConnectionOut])
async def list_connections(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[MailboxConnection]:
    # Read-only for any active member (Media needs it to pick a connection
    # when creating a session; the rest of the team can see it too, same
    # reasoning as audit-logs/reconciliation) -- only create/revoke below
    # stay Owner/Finance-only. Nothing sensitive is exposed here (no
    # token_ref/webhook_secret/imap_password in the schema).
    result = await db.execute(
        select(MailboxConnection).where(MailboxConnection.organization_id == organization_id)
    )
    return list(result.scalars().all())


@router.post("/connections/{connection_id}/revoke", response_model=MailboxConnectionOut)
async def revoke_connection(
    organization_id: str,
    connection_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MailboxConnection:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    connection = await db.get(MailboxConnection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")

    connection.status = ConnectionStatus.REVOKED
    connection.token_ref = None

    await record_audit_event(
        db,
        action="connection.revoked",
        target_type="mailbox_connection",
        target_id=connection.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(connection)
    imap_idle.stop_watching(connection.id)
    return connection


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    organization_id: str,
    connection_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Permanently removes a connection from the list -- only once it's
    already revoked (revoke first is a deliberate two-step, same shape as
    every other destructive action in this app) and only if it never
    actually ingested a real deposit. ContributionEvent.mailbox_connection_id
    is a real foreign key with no cascade behavior configured on purpose --
    hard-deleting a connection that ledger rows still point at would either
    fail outright or (worse, on a database that doesn't enforce the
    constraint) silently orphan real donation history. A connection that's
    never processed anything (created by mistake, or the wrong mailbox) has
    no such history to protect, so deleting it is unconditionally safe.
    """
    require_roles(membership, Role.OWNER, Role.FINANCE)

    connection = await db.get(MailboxConnection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    if connection.status != ConnectionStatus.REVOKED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a revoked connection can be deleted. Revoke it first.",
        )

    result = await db.execute(
        select(ContributionEvent.id)
        .where(ContributionEvent.mailbox_connection_id == connection_id)
        .limit(1)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This connection has processed real deposit events and can't be deleted, to "
                "keep the ledger's history intact. It's already revoked and hidden from new "
                "sessions."
            ),
        )

    await record_audit_event(
        db,
        action="connection.deleted",
        target_type="mailbox_connection",
        target_id=connection.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        before={"provider": connection.provider.value, "mailbox": connection.mailbox},
        ip_address=_client_ip(request),
    )

    await db.delete(connection)
    await db.commit()
    imap_idle.stop_watching(connection_id)


@router.post("/connections/{connection_id}/check-now", response_model=ImapCheckNowResponse)
async def check_connection_now(
    organization_id: str,
    connection_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> ImapCheckNowResponse:
    """Runs an IMAP poll immediately instead of waiting for the next
    scheduled cycle (app/main.py's background loop, every 60s) -- so
    connecting a mailbox and clicking "Check now" gives instant feedback
    rather than a silent wait.
    """
    require_roles(membership, Role.OWNER, Role.FINANCE, Role.MEDIA)

    connection = await db.get(MailboxConnection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    if connection.provider != MailboxProviderName.IMAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only IMAP connections can be checked on demand.",
        )
    if connection.status != ConnectionStatus.CONNECTED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Connection is not active.")

    summary = await poll_imap_connection(db, connection)
    return ImapCheckNowResponse(fetched=summary.fetched, accepted=summary.accepted, error=summary.error)


@router.get(
    "/connections/{connection_id}/recent-messages", response_model=list[RecentImapMessageOut]
)
async def list_recent_imap_messages(
    organization_id: str,
    connection_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[RecentImapMessageOut]:
    """Raw fetched-message diagnostic trail (not run through/gated by the
    parser's own decision) for an Owner/Finance officer to see exactly what
    the poller found and how it was judged, without guessing blind -- see
    app/domain/imap_polling.py's in-memory recent-message log for why this
    is deliberately never persisted to the database. Most recent first,
    capped, resets on server restart.
    """
    require_roles(membership, Role.OWNER, Role.FINANCE)

    connection = await db.get(MailboxConnection, connection_id)
    if connection is None or connection.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found.")
    if connection.provider != MailboxProviderName.IMAP:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only IMAP connections have a fetched-message log.",
        )

    return [
        RecentImapMessageOut(
            uid=entry.uid,
            fetched_at=entry.fetched_at,
            received_at=entry.received_at,
            sender=entry.sender,
            subject=entry.subject,
            body_snippet=entry.body_snippet,
            decision=entry.decision,
            decision_reason=entry.decision_reason,
        )
        for entry in get_recent_messages(connection_id)
    ]


@router.post(
    "/parser-profiles", response_model=ParserProfileOut, status_code=status.HTTP_201_CREATED
)
async def create_parser_profile(
    organization_id: str,
    payload: ParserProfileCreateRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParserProfile:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    profile = ParserProfile(
        organization_id=organization_id,
        name=payload.name,
        template_version=payload.template_version,
        sender_patterns=payload.sender_patterns,
        credit_keywords=payload.credit_keywords or list(DEFAULT_CREDIT_KEYWORDS),
        reject_keywords=payload.reject_keywords or list(DEFAULT_REJECT_KEYWORDS),
        amount_pattern=payload.amount_pattern or DEFAULT_AMOUNT_PATTERN,
        default_currency=payload.default_currency.upper(),
        confidence_threshold=payload.confidence_threshold,
    )
    db.add(profile)
    await db.flush()

    await record_audit_event(
        db,
        action="parser_profile.created",
        target_type="parser_profile",
        target_id=profile.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        after={"name": profile.name, "sender_patterns": profile.sender_patterns},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(profile)
    return profile


@router.get("/parser-profiles", response_model=list[ParserProfileOut])
async def list_parser_profiles(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[ParserProfile]:
    # Read-only for any active member -- only create/update/delete below
    # stay Owner/Finance-only.
    result = await db.execute(
        select(ParserProfile).where(ParserProfile.organization_id == organization_id)
    )
    return list(result.scalars().all())


@router.patch("/parser-profiles/{profile_id}", response_model=ParserProfileOut)
async def update_parser_profile(
    organization_id: str,
    profile_id: str,
    payload: ParserProfileUpdateRequest,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ParserProfile:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    profile = await db.get(ParserProfile, profile_id)
    if profile is None or profile.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parser profile not found.")

    before = {
        "name": profile.name,
        "sender_patterns": profile.sender_patterns,
        "is_active": profile.is_active,
    }

    updates = payload.model_dump(exclude_unset=True)
    if "default_currency" in updates and updates["default_currency"] is not None:
        updates["default_currency"] = updates["default_currency"].upper()
    for field, value in updates.items():
        setattr(profile, field, value)

    await record_audit_event(
        db,
        action="parser_profile.updated",
        target_type="parser_profile",
        target_id=profile.id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        before=before,
        after={"name": profile.name, "sender_patterns": profile.sender_patterns, "is_active": profile.is_active},
        ip_address=_client_ip(request),
    )

    await db.commit()
    await db.refresh(profile)
    return profile


@router.delete("/parser-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_parser_profile(
    organization_id: str,
    profile_id: str,
    request: Request,
    membership: Membership = Depends(get_membership),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    require_roles(membership, Role.OWNER, Role.FINANCE)

    profile = await db.get(ParserProfile, profile_id)
    if profile is None or profile.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Parser profile not found.")

    # Safe to hard-delete: nothing references a parser profile by id --
    # ContributionEvent only copies its template_version string at ingest
    # time (see app/domain/ingestion.py), so historical ledger rows are
    # completely unaffected by a profile being deleted later.
    await db.delete(profile)

    await record_audit_event(
        db,
        action="parser_profile.deleted",
        target_type="parser_profile",
        target_id=profile_id,
        organization_id=organization_id,
        actor_user_id=current_user.id,
        before={"name": profile.name, "sender_patterns": profile.sender_patterns},
        ip_address=_client_ip(request),
    )

    await db.commit()
