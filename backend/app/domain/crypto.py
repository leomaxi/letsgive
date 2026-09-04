from cryptography.fernet import Fernet
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().encryption_key.encode())


class EncryptedString(TypeDecorator):
    """Envelope-encrypts a string column at rest with Fernet (AES-128-CBC +
    HMAC-SHA256, authenticated). Application code always sees the plaintext
    -- encryption/decryption happens transparently at the SQLAlchemy layer,
    the same way UTCDateTime (app/db/base.py) normalizes timestamps -- so no
    call site needs to change. Protects secrets like mfa_secret and
    webhook_secret if the database itself is ever exposed (backup leak,
    misconfigured read access), without that exposure alone being enough to
    reconstruct a user's MFA seed or forge a webhook signature.

    Ciphertext is meaningfully longer than plaintext (Fernet's own overhead
    plus base64), so a column using this type needs a generous `impl` length
    -- see migration 0006 for the two columns this widened.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(value.encode()).decode()

    def process_result_value(self, value: str | None, dialect) -> str | None:
        if value is None:
            return None
        return _fernet().decrypt(value.encode()).decode()
