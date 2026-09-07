import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token, decode_display_token
from app.db.models.membership import Membership, MembershipStatus
from app.db.models.session import Session
from app.db.models.user import User
from app.db.session import get_db

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login", auto_error=False)
display_token_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_error
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
    except jwt.PyJWTError as exc:
        raise credentials_error from exc
    if not user_id:
        raise credentials_error

    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error
    return user


async def load_active_membership(
    db: AsyncSession, user_id: str, organization_id: str
) -> Membership | None:
    result = await db.execute(
        select(Membership).where(
            Membership.user_id == user_id,
            Membership.organization_id == organization_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        return None
    return membership


async def get_membership(
    organization_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Membership:
    membership = await load_active_membership(db, current_user.id, organization_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found.",
        )
    return membership


async def get_platform_admin(current_user: User = Depends(get_current_user)) -> User:
    """Gate for app/api/v1/admin.py -- cross-tenant routes with no single
    organization_id in their path, so there's no tenant-existence secrecy
    concern the way get_membership's 404 protects; a plain 403 is correct.
    """
    if not current_user.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform admin access required.",
        )
    return current_user


async def get_session_membership(
    session_id: str = Path(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> tuple[Session, Membership]:
    not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")

    session = await db.get(Session, session_id)
    if session is None:
        raise not_found

    result = await db.execute(
        select(Membership).where(
            Membership.user_id == current_user.id,
            Membership.organization_id == session.organization_id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        # Tenant isolation: a non-member gets the same 404 as a nonexistent
        # session id, never a 403 that would confirm the session exists.
        raise not_found
    return session, membership


async def get_display_session(
    session_id: str = Path(...),
    token: str | None = Depends(display_token_scheme),
    db: AsyncSession = Depends(get_db),
) -> Session:
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing display token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise invalid
    try:
        token_session_id = decode_display_token(token)
    except jwt.PyJWTError as exc:
        raise invalid from exc
    if token_session_id != session_id:
        raise invalid

    session = await db.get(Session, session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found.")
    return session
