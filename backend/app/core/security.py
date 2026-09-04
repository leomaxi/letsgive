import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False


def generate_otp_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


# Same Argon2 hasher as passwords -- the code's small keyspace means the real
# defense is expiry + attempt-lockout (see Approval), not hash strength.
hash_otp_code = hash_password
verify_otp_code = verify_password


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def generate_mfa_secret() -> str:
    return pyotp.random_base32()


def mfa_provisioning_uri(secret: str, account_email: str) -> str:
    settings = get_settings()
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_email, issuer_name=settings.mfa_issuer)


def verify_mfa_code(secret: str, code: str) -> bool:
    return pyotp.totp.TOTP(secret).verify(code, valid_window=1)


DISPLAY_TOKEN_TYPE = "display"


def create_display_token(session_id: str, expire_minutes: int = 240) -> str:
    """Short-lived, session-scoped token for the public projection client.

    Carries no user identity or finance permissions (spec 11.1), and its
    distinct 'typ' claim keeps it from being usable as a normal user bearer
    token even though it's signed with the same secret.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sid": session_id,
        "typ": DISPLAY_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_display_token(token: str) -> str:
    """Returns the session_id the token is scoped to, or raises jwt.PyJWTError."""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != DISPLAY_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Not a display token.")
    return payload["sid"]


OPERATOR_SOCKET_TOKEN_TYPE = "operator_socket"


def create_operator_socket_token(session_id: str, user_id: str, expire_minutes: int = 60) -> str:
    """Short-lived, session+user-scoped token for the operator console's
    realtime channel. A browser WebSocket handshake can't carry a custom
    Authorization header, so -- same reasoning as the public display token
    above -- this is a narrow, short-lived credential safe to put in a URL
    query string, rather than putting the general-purpose access token there.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sid": session_id,
        "sub": user_id,
        "typ": OPERATOR_SOCKET_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_operator_socket_token(token: str) -> tuple[str, str]:
    """Returns (session_id, user_id), or raises jwt.PyJWTError."""
    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != OPERATOR_SOCKET_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Not an operator socket token.")
    return payload["sid"], payload["sub"]
