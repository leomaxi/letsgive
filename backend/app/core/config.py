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
    smtp_from_email: str = "noreply@letsgive.ca"
    smtp_use_tls: bool = True

    # How often app/main.py's background loop re-checks every connected IMAP
    # mailbox for new deposit notifications (spec 9 pull, IMAP has no webhook
    # of its own). Lower = donors see their gift counted sooner during a live
    # session, at the cost of one more IMAP login/logout cycle per connected
    # mailbox each interval -- most providers (Gmail included) treat a fresh
    # login as a security-relevant event, so don't drop this to single-digit
    # seconds without a reason.
    imap_poll_interval_seconds: int = 15

    # How often app/main.py's background loop checks for organizations whose
    # admin-assigned plan_expires_at has passed (see app/domain/subscriptions.py
    # ::revert_expired_plans). A scheduled plan grant lapsing an hour late
    # isn't operationally meaningful the way a missed live deposit would be,
    # so this doesn't need imap_poll_interval_seconds-level urgency.
    plan_expiry_check_interval_seconds: int = 1800


@lru_cache
def get_settings() -> Settings:
    return Settings()
