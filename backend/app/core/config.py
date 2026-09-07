from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="LETSGIVE_", extra="ignore")

    env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./letsgive_dev.db"

    jwt_secret: str = "dev-secret-change-me-to-a-random-32-byte-value"
    jwt_algorithm: str = "HS256"
    # A church service or giving event routinely runs longer than an hour;
    # an Owner/Media/Finance operator getting silently logged out mid-session
    # is disruptive, not a meaningful security improvement (rotating
    # LETSGIVE_JWT_SECRET already invalidates every outstanding token
    # instantly if one ever needs to be revoked early). 480 = 8 hours, a full
    # day's use without needing to sign back in.
    access_token_expire_minutes: int = 480

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
    # mailbox for new deposit notifications. This is now only a slower
    # safety net, not the primary mechanism -- real-time detection comes
    # from per-mailbox IMAP IDLE watchers (app/domain/imap_idle.py), which
    # get notified by the server the instant new mail arrives instead of
    # asking repeatedly. This loop exists as defense in depth for a watcher
    # that silently died, and as a backstop for a provider that doesn't
    # support IDLE at all -- it doesn't need IDLE-level urgency.
    imap_poll_interval_seconds: int = 300

    # How often app/main.py's background loop checks for organizations whose
    # admin-assigned plan_expires_at has passed (see app/domain/subscriptions.py
    # ::revert_expired_plans). A scheduled plan grant lapsing an hour late
    # isn't operationally meaningful the way a missed live deposit would be,
    # so this doesn't need imap_poll_interval_seconds-level urgency.
    plan_expiry_check_interval_seconds: int = 1800


@lru_cache
def get_settings() -> Settings:
    return Settings()
