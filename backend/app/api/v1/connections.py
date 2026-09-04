from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_membership
from app.api.v1.schemas import (
    MailboxConnectionCreateRequest,
    MailboxConnectionOut,
    ParserProfileCreateRequest,
    ParserProfileOut,
)
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection
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
