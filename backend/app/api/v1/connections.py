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
)
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
from app.domain.audit import record_audit_event
from app.domain.billing import assert_can_create_connection
from app.domain.imap_polling import poll_imap_connection
from app.domain.imap_provider import ImapAuthError, guess_imap_host, verify_imap_login
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
            await verify_imap_login(host=host, port=port, mailbox=payload.mailbox, password=payload.imap_password)
        except ImapAuthError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        connection.imap_host = host
        connection.imap_port = port
        connection.imap_password = payload.imap_password
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
    return connection


@router.get("/connections", response_model=list[MailboxConnectionOut])
async def list_connections(
    organization_id: str,
    membership: Membership = Depends(get_membership),
    db: AsyncSession = Depends(get_db),
) -> list[MailboxConnection]:
    # Media needs read access to pick a connection when creating a session,
    # even though only Owner/Finance can create or revoke one. Nothing
    # sensitive is exposed here (no token_ref/webhook_secret in the schema).
    require_roles(membership, Role.OWNER, Role.FINANCE, Role.MEDIA)
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
    return connection


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
    require_roles(membership, Role.OWNER, Role.FINANCE)
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
