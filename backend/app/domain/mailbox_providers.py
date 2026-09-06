import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.db.models.mailbox_connection import MailboxConnection, MailboxProviderName


@dataclass
class AuthorizationStart:
    status: str  # "connected" (dev provider completes immediately) or "pending_redirect"
    authorization_url: str | None
    external_account_id: str | None = None
    token_ref: str | None = None


@dataclass
class RawMessage:
    provider_message_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime


class MailboxProvider(Protocol):
    name: MailboxProviderName

    async def begin_authorization(
        self, *, organization_id: str, mailbox: str, folder: str | None
    ) -> AuthorizationStart: ...

    def verify_webhook_signature(
        self, *, connection: MailboxConnection, raw_body: bytes, signature: str
    ) -> bool: ...

    async def fetch_message(
        self, *, connection: MailboxConnection, provider_message_id: str
    ) -> RawMessage | None: ...


def sign_webhook_body(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


class BaseSignedProvider:
    """Shared HMAC-SHA256 signature check used by every provider adapter here.

    Real Microsoft Graph / Gmail push notifications use their own provider-
    specific validation (clientState comparison, Pub/Sub JWT verification) --
    this stands in for that until the Phase 3-hardening work wires up the
    real per-provider schemes; see FakeMailboxProvider below for what's
    actually exercised today.
    """

    def verify_webhook_signature(
        self, *, connection: MailboxConnection, raw_body: bytes, signature: str
    ) -> bool:
        expected = sign_webhook_body(connection.webhook_secret, raw_body)
        return hmac.compare_digest(expected, signature)


@dataclass
class FakeMailboxProvider(BaseSignedProvider):
    """Dev/test double: no network calls, no external credentials.

    Authorization completes immediately (no browser redirect needed), and
    fetch_message returns whatever a test/dev caller seeded via
    seed_message() -- standing in for the real provider's message-fetch API.
    """

    name: MailboxProviderName = MailboxProviderName.FAKE
    _mailbox: dict[tuple[str, str], RawMessage] = field(default_factory=dict)

    async def begin_authorization(
        self, *, organization_id: str, mailbox: str, folder: str | None
    ) -> AuthorizationStart:
        return AuthorizationStart(
            status="connected",
            authorization_url=None,
            external_account_id=f"fake-account-{mailbox}",
            token_ref=f"fake-token-ref-{mailbox}",
        )

    def seed_message(self, connection_id: str, message: RawMessage) -> None:
        self._mailbox[(connection_id, message.provider_message_id)] = message

    def clear(self) -> None:
        self._mailbox.clear()

    async def fetch_message(
        self, *, connection: MailboxConnection, provider_message_id: str
    ) -> RawMessage | None:
        return self._mailbox.get((connection.id, provider_message_id))


class _NotYetImplementedProvider(BaseSignedProvider):
    """Real Microsoft Graph / Gmail integration is a Phase 3-hardening item:
    it needs a registered OAuth app, live credentials, and the provider SDK,
    none of which exist in this environment. The interface is wired end to
    end so plugging in real HTTP calls here is the only remaining step.
    """

    def __init__(self, name: MailboxProviderName):
        self.name = name

    async def begin_authorization(
        self, *, organization_id: str, mailbox: str, folder: str | None
    ) -> AuthorizationStart:
        raise NotImplementedError(
            f"{self.name.value} OAuth is not wired up yet -- needs a registered app "
            "and live credentials (see app/domain/mailbox_providers.py)."
        )

    async def fetch_message(
        self, *, connection: MailboxConnection, provider_message_id: str
    ) -> RawMessage | None:
        raise NotImplementedError(f"{self.name.value} message fetch is not wired up yet.")


class _ImapNotAWebhookProvider(BaseSignedProvider):
    """IMAP connections are pull-based (see app/domain/imap_polling.py) and
    are created directly in app/api/v1/connections.py rather than through
    begin_authorization() -- this entry exists only so a stray call to
    get_provider(IMAP) (e.g. POST /v1/providers/imap/webhook, which makes no
    sense for a provider with no webhook) fails with a clear message instead
    of a raw KeyError.
    """

    name: MailboxProviderName = MailboxProviderName.IMAP

    async def begin_authorization(
        self, *, organization_id: str, mailbox: str, folder: str | None
    ) -> AuthorizationStart:
        raise NotImplementedError(
            "IMAP connections are created via POST .../connections with imap_password set, "
            "not through this OAuth-style authorization flow."
        )

    async def fetch_message(
        self, *, connection: MailboxConnection, provider_message_id: str
    ) -> RawMessage | None:
        raise NotImplementedError("IMAP messages are polled, not fetched by id -- see imap_polling.py.")


_PROVIDERS: dict[MailboxProviderName, MailboxProvider] = {
    MailboxProviderName.FAKE: FakeMailboxProvider(),
    MailboxProviderName.MICROSOFT: _NotYetImplementedProvider(MailboxProviderName.MICROSOFT),
    MailboxProviderName.GMAIL: _NotYetImplementedProvider(MailboxProviderName.GMAIL),
    MailboxProviderName.IMAP: _ImapNotAWebhookProvider(),
}


def get_provider(name: MailboxProviderName) -> MailboxProvider:
    return _PROVIDERS[name]


def get_fake_provider() -> FakeMailboxProvider:
    """Test/dev helper for seeding messages directly."""
    provider = _PROVIDERS[MailboxProviderName.FAKE]
    assert isinstance(provider, FakeMailboxProvider)
    return provider
