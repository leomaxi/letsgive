from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.api.v1.schemas import (
    LoginRequest,
    MfaActivateRequest,
    MfaEnrollResponse,
    TokenResponse,
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
from app.db.session import get_db
from app.domain.rate_limit import login_ip_rate_limiter, login_rate_limiter

router = APIRouter(prefix="/v1/auth", tags=["auth"])


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


@router.get("/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)) -> User:
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
