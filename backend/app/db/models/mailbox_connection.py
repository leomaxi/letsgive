import enum
import secrets
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.domain.crypto import EncryptedString


class MailboxProviderName(str, enum.Enum):
    FAKE = "fake"
    MICROSOFT = "microsoft"
    GMAIL = "gmail"
    IMAP = "imap"


class ConnectionStatus(str, enum.Enum):
    PENDING = "pending"
    CONNECTED = "connected"
    ERROR = "error"
    REVOKED = "revoked"


def _generate_webhook_secret() -> str:
    return secrets.token_urlsafe(32)


class MailboxConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A dedicated mailbox/folder an organization has authorized for monitoring.

    token_ref is a reference into a secret store, never the raw OAuth token
    (spec 8: "Secrets") -- it stores an opaque placeholder since real OAuth
    hasn't landed yet (see README known gaps), so there's no plaintext token
    in this table to encrypt in the first place. webhook_secret, which *is*
    real secret material generated and used today, is encrypted at rest
    (see app/domain/crypto.py) -- the envelope encryption spec 8 calls for.
    """

    __tablename__ = "mailbox_connections"

    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    provider: Mapped[MailboxProviderName] = mapped_column(
        SAEnum(
            MailboxProviderName,
            name="mailbox_provider",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    mailbox: Mapped[str] = mapped_column(String(255), nullable=False)
    folder: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[ConnectionStatus] = mapped_column(
        SAEnum(
            ConnectionStatus,
            name="connection_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ConnectionStatus.PENDING,
    )
    token_ref: Mapped[str | None] = mapped_column(String(255))
    external_account_id: Mapped[str | None] = mapped_column(String(255))
    webhook_secret: Mapped[str] = mapped_column(
        EncryptedString(500), nullable=False, default=_generate_webhook_secret
    )

    last_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    webhook_health: Mapped[str | None] = mapped_column(String(255))
    is_dedicated_mailbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # IMAP ("log in with your email") connections only -- see
    # app/domain/imap_provider.py. imap_password is the user's app-specific
    # password, encrypted at rest the same way webhook_secret already is.
    imap_host: Mapped[str | None] = mapped_column(String(255))
    imap_port: Mapped[int | None] = mapped_column(Integer)
    imap_password: Mapped[str | None] = mapped_column(EncryptedString(500))
