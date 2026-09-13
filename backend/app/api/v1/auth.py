import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ensure_utc, utcnow
from app.api.v1.deps import get_current_user
from app.api.v1.schemas import (
    LoginRequest,
    MessageResponse,
    MfaActivateRequest,
    MfaEnrollResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    TokenResponse,
    UserEmailUpdateRequest,
    UserOut,
    UserRegisterRequest,
)
from app.core.security import (
    create_access_token,
    generate_mfa_secret,
    hash_password,
    mfa_provisioning_uri,
    verify_mfa_code,
    verify_password,
)
from app.db.models.user import User
from app.db.models.password_reset_token import PasswordResetToken
from app.db.session import get_db
from app.domain.notifications import Notifier, get_notifier
from app.domain.rate_limit import login_ip_rate_limiter, login_rate_limiter

router = APIRouter(prefix="/v1/auth", tags=["auth"])

PASSWORD_RESET_TTL_MINUTES = 30
PASSWORD_RESET_GENERIC_MESSAGE = (
    "If an account exists for that email, a password reset link has been sent."
)


def _hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _reset_url_for_request(request: Request, token: str) -> str:
    origin = request.headers.get("origin")
    base_url = origin.rstrip("/") if origin else str(request.base_url).rstrip("/")
    return f"{base_url}/reset-password?token={token}"


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegisterRequest, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.execute(select(User).where(User.email == payload.email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered.")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    client_ip = request.client.host if request.client else "unknown"
    login_ip_rate_limiter.check(f"login-ip:{client_ip}")

    rate_limit_key = f"login:{payload.email.lower()}"
    login_rate_limiter.check(rate_limit_key)

    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password."
    )

    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(payload.password, user.hashed_password):
        raise invalid_credentials

    if user.mfa_enabled:
        if not payload.mfa_code:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="mfa_required")
        if not verify_mfa_code(user.mfa_secret, payload.mfa_code):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="mfa_invalid")

    login_rate_limiter.reset(rate_limit_key)
    token = create_access_token(subject=user.id)
    return TokenResponse(access_token=token)


@router.post("/password-reset/request", response_model=MessageResponse)
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    notifier: Notifier = Depends(get_notifier),
) -> MessageResponse:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        return MessageResponse(message=PASSWORD_RESET_GENERIC_MESSAGE)

    raw_token = secrets.token_urlsafe(32)
    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_reset_token(raw_token),
        expires_at=utcnow() + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES),
    )
    db.add(reset)
    await db.commit()

    await notifier.send_password_reset(
        to_email=user.email,
        reset_url=_reset_url_for_request(request, raw_token),
    )
    return MessageResponse(message=PASSWORD_RESET_GENERIC_MESSAGE)


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest, db: AsyncSession = Depends(get_db)
) -> MessageResponse:
    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash_reset_token(payload.token))
    )
    reset = result.scalar_one_or_none()
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This password reset link is invalid or has expired.",
    )
    if reset is None or reset.used_at is not None or ensure_utc(reset.expires_at) < utcnow():
        raise invalid

    user = await db.get(User, reset.user_id)
    if user is None or not user.is_active:
        raise invalid

    user.hashed_password = hash_password(payload.password)
    now = utcnow()
    outstanding = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
    )
    for token in outstanding.scalars().all():
        token.used_at = now

    await db.commit()
    return MessageResponse(message="Password updated. You can sign in with your new password.")


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/me/email", response_model=UserOut)
async def update_my_email(
    payload: UserEmailUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect.")

    new_email = str(payload.email)
    if new_email == current_user.email:
        return current_user

    existing = await db.execute(select(User).where(User.email == new_email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered.")

    current_user.email = new_email
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.post("/mfa/enroll", response_model=MfaEnrollResponse)
async def enroll_mfa(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> MfaEnrollResponse:
    if current_user.mfa_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="MFA already enabled.")
    secret = generate_mfa_secret()
    current_user.mfa_secret = secret
    await db.commit()
    return MfaEnrollResponse(
        secret=secret,
        provisioning_uri=mfa_provisioning_uri(secret, current_user.email),
    )


@router.post("/mfa/activate", status_code=status.HTTP_204_NO_CONTENT)
async def activate_mfa(
    payload: MfaActivateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not current_user.mfa_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="MFA has not been enrolled.")
    if not verify_mfa_code(current_user.mfa_secret, payload.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code.")
    current_user.mfa_enabled = True
    await db.commit()
