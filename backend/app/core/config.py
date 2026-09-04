from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LETSGIVE_", extra="ignore")

    env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./letsgive_dev.db"

    jwt_secret: str = "dev-secret-change-me-to-a-random-32-byte-value"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    mfa_issuer: str = "Let's Give"

    # Envelope-encrypts a handful of at-rest secret columns (mfa_secret,
    # webhook_secret -- see app/domain/crypto.py). Must be a urlsafe-base64
    # 32-byte key in the format `cryptography.fernet.Fernet.generate_key()`
    # produces; this dev default is exactly that, just committed (fine for
    # dev/test, never for a real deployment -- same rule as jwt_secret above).
    encryption_key: str = "aDitUzqDj29Jdbp2iOLCrl8mbC4Q7lUGtb_x9Bzq1Kw="

    # Real email delivery for finance-approval OTP codes (spec 5.1
    # "notifications"). Empty smtp_host (the default) means "not configured"
    # -- app/domain/notifications.py falls back to logging the code instead
    # of trying to send it, so a fresh dev checkout works with zero setup.
    # Set LETSGIVE_SMTP_HOST (and friends) to switch to real delivery.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@letsgive.pellutech.com"
    smtp_use_tls: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
