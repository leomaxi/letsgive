import asyncio
import email
import email.utils
import hashlib
import html
import imaplib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.header import decode_header, make_header

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models.contribution_event import ContributionDecision
from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection, MailboxProviderName
from app.db.models.session import Session
from app.domain.ingestion import IngestResult, ingest_message
from app.domain.mailbox_providers import RawMessage

logger = logging.getLogger("letsgive.imap")
# uvicorn's default logging config doesn't attach a handler to arbitrary app
# loggers, so INFO messages would silently vanish both in local dev and in
# production (the only reason exception/warning-level logs from this module
# have ever been visible in journalctl is Python's WARNING+-only last-resort
# fallback handler) -- same fix already used in app/domain/notifications.py.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# Every trigger that can fetch a given connection -- the manual "check now"
# button, the IDLE-triggered catch-up (app/domain/imap_idle.py), and the
# slower fallback poll loop (app/main.py) -- funnels through
# poll_imap_connection below, so a single lock registry here makes all of
# them mutually exclusive per mailbox with no call site able to forget it.
# A real production 500 was traced to two of these racing the same
# connection's IMAP session/watermark update at once.
_connection_locks: dict[str, asyncio.Lock] = {}


def _lock_for(connection_id: str) -> asyncio.Lock:
    lock = _connection_locks.get(connection_id)
    if lock is None:
        lock = asyncio.Lock()
        _connection_locks[connection_id] = lock
    return lock


# In-memory only, deliberately never written to the database: a rolling
# per-connection diagnostic trail of exactly what the IMAP poller fetched
# and how the parser judged it, for an Owner/Finance officer to see *why* a
# real deposit wasn't counted without guessing blind (see
# GET .../connections/{id}/recent-messages in app/api/v1/connections.py).
# Not persisted on purpose -- ContributionEvent deliberately has no
# sender/subject/body column at all (spec 8, data minimization); this is a
# live debugging aid for the same Owner/Finance roles who already receive
# these emails directly in their own inbox, not a new place donor content
# ends up retained. Resets on process restart and is capped per connection
# so it can never grow into a de facto unbounded copy of the mailbox.
_RECENT_LOG_MAX_PER_CONNECTION = 25
_recent_messages: dict[str, deque] = {}


@dataclass
class RecentMessageLogEntry:
    uid: str
    fetched_at: datetime
    received_at: datetime
    sender: str
    subject: str
    body_snippet: str
    decision: str
    decision_reason: str | None


def _record_recent_message(
    connection_id: str, *, uid: str, message: RawMessage, decision: str, reason: str | None
) -> None:
    buffer = _recent_messages.setdefault(
        connection_id, deque(maxlen=_RECENT_LOG_MAX_PER_CONNECTION)
    )
    buffer.appendleft(
        RecentMessageLogEntry(
            uid=uid,
            fetched_at=utcnow(),
            received_at=message.received_at,
            sender=message.sender,
            subject=message.subject,
            body_snippet=message.body[:500],
            decision=decision,
            decision_reason=reason,
        )
    )


def get_recent_messages(connection_id: str) -> list[RecentMessageLogEntry]:
    return list(_recent_messages.get(connection_id, []))


@dataclass
class _FetchedMessage:
    uid: str
    message: RawMessage


def _decode_str(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 -- a malformed header shouldn't crash ingestion
        return value


def _html_to_text(raw_html: str) -> str:
    """Strips tags to recover searchable plain text from an HTML email body.
    Not a real renderer -- just enough for the keyword/amount matching in
    app/domain/parsing.py to find real text, which is all that's needed here.
    Tags are replaced with a space (not deleted outright) so adjacent table
    cells like "<td>$1.00</td><td>(CAD)</td>" don't fuse into "$1.00(CAD)".
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _extract_body(msg: email.message.Message) -> str:
    """Real bank/Interac notification templates are routinely HTML-only (or
    multipart/alternative with a bare-bones plain-text stub that omits the
    actual amount/details) -- a real deposit notification was missed in
    production because this only ever looked at text/plain and silently gave
    up with "" the moment that part was absent or a stub, even though the
    HTML part right next to it had the real content. Both parts are now
    collected and concatenated: whichever one turns out to have the real
    text, the keyword/amount search in parsing.py will find it either way.
    """
    if not msg.is_multipart():
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)
        if payload is None:
            return ""
        decoded = payload.decode(charset, errors="replace")
        return _html_to_text(decoded) if content_type == "text/html" else decoded

    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded = part.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if content_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(_html_to_text(decoded))

    return "\n".join(plain_parts + html_parts)


def _parse_message(uid: bytes, raw_bytes: bytes) -> _FetchedMessage:
    msg = email.message_from_bytes(raw_bytes)
    sender = email.utils.parseaddr(msg.get("From", ""))[1] or msg.get("From", "unknown")
    subject = _decode_str(msg.get("Subject"))

    received_at = datetime.now(timezone.utc)
    date_header = msg.get("Date")
    if date_header:
        try:
            parsed = email.utils.parsedate_to_datetime(date_header)
            if parsed is not None:
                received_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                received_at = received_at.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass

    provider_message_id = msg.get("Message-ID")
    if not provider_message_id:
        # Exceedingly rare for real mail, but fall back to something stable
        # for this mailbox rather than crashing ingestion over a missing header.
        provider_message_id = "imap-" + hashlib.sha256(raw_bytes).hexdigest()[:32]

    return _FetchedMessage(
        uid=uid.decode() if isinstance(uid, bytes) else str(uid),
        message=RawMessage(
            provider_message_id=provider_message_id.strip(),
            sender=sender,
            subject=subject,
            body=_extract_body(msg),
            received_at=received_at,
        ),
    )


def _fetch_new_sync(
    *, host: str, port: int, mailbox: str, password: str, since_uid: int | None
) -> tuple[list[_FetchedMessage], int | None]:
    """Fetches every message with a UID greater than since_uid (or, when
    since_uid is None -- a connection that predates this watermark existing
    -- every message in the mailbox, once, as a one-time catch-up sweep).

    UID, not the \\Seen flag, is the source of truth for "have we looked at
    this message" (see poll_imap_connection's docstring for why the \\Seen
    flag alone was never safe to rely on). Returns the highest UID actually
    examined alongside the fetched messages, so the caller can advance the
    watermark past *everything looked at* -- including a message that fails
    to parse -- not just the ones that were successfully ingested; otherwise
    a single malformed message would wedge every poll into re-fetching it
    forever.
    """
    connection = imaplib.IMAP4_SSL(host, port, timeout=15)
    try:
        connection.login(mailbox, password)
        connection.select("INBOX")

        if since_uid is None:
            typ, data = connection.uid("search", None, "ALL")
        else:
            # IMAP's "N:*" range has a real, documented quirk (RFC 3501):
            # if N is higher than every UID in the mailbox, some servers
            # return the *last* message instead of an empty result. Search
            # inclusive of since_uid itself and filter it (and anything
            # <=) back out below, rather than trusting "N+1:*" to behave.
            typ, data = connection.uid("search", None, f"UID {since_uid}:*")

        if typ != "OK" or not data or not data[0]:
            return [], None

        uids = [int(u) for u in data[0].split()]
        if since_uid is not None:
            uids = [u for u in uids if u > since_uid]
        if not uids:
            return [], None

        fetched: list[_FetchedMessage] = []
        for uid in uids:
            # BODY.PEEK[] (not BODY[]/RFC822) so fetching never marks a
            # message \Seen on its own -- that's purely a courtesy to a
            # human glancing at the mailbox (see _mark_seen_sync), not
            # something ingestion tracking depends on anymore.
            typ, msg_data = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw_bytes = msg_data[0][1]
            try:
                fetched.append(_parse_message(str(uid).encode(), raw_bytes))
            except Exception:  # noqa: BLE001
                logger.exception("Failed to parse IMAP message uid=%s from %s", uid, mailbox)
        return fetched, max(uids)
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


def _mark_seen_sync(*, host: str, port: int, mailbox: str, password: str, uids: list[str]) -> None:
    if not uids:
        return
    connection = imaplib.IMAP4_SSL(host, port, timeout=15)
    try:
        connection.login(mailbox, password)
        connection.select("INBOX")
        connection.uid("store", ",".join(uids), "+FLAGS", "\\Seen")
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class ImapPollSummary:
    fetched: int
    accepted: int
    error: str | None = None


async def poll_imap_connection(db: AsyncSession, connection: MailboxConnection) -> ImapPollSummary:
    """Fetches new messages from a connected IMAP mailbox and runs each
    through the same ingest_message pipeline the webhook path uses (spec 9.1)
    -- IMAP is pull-based instead of push, but everything downstream of
    "here is a RawMessage" is identical.

    "New" is tracked with connection.imap_last_uid, a real persisted
    watermark -- not the IMAP \\Seen flag. An earlier version of this used
    "search UNSEEN" as its only tracking mechanism, which broke the moment
    anything else touched the mailbox's read state: a human glancing at a
    real deposit-notification inbox on their phone marks it \\Seen, and a
    real deposit was silently never picked up because of it. The watermark
    also makes this correctly idempotent even if ingestion or the \\Seen
    mark-back below fails partway through -- see ingest_message for the
    dedup guarantee that makes re-examining an already-ingested UID safe.

    Every caller (the manual check-now endpoint, the IDLE-triggered catch-up,
    the slower fallback poll loop) is serialized per connection via the lock
    below -- two triggers racing the same mailbox's IMAP session/watermark at
    once produced a real unhandled-exception 500 in production.
    """
    lock_wait_started = time.monotonic()
    async with _lock_for(connection.id):
        lock_wait_seconds = time.monotonic() - lock_wait_started
        if lock_wait_seconds > 1:
            # Distinguishes "the fetch itself was slow" from "this trigger
            # sat queued behind another one" -- the two look identical from
            # outside, but only one of them means the lock/scheduling is the
            # bottleneck rather than the IMAP round trip itself.
            logger.info(
                "IMAP poll for connection %s waited %.1fs for the per-connection lock",
                connection.id,
                lock_wait_seconds,
            )
        return await _poll_imap_connection_locked(db, connection)


async def _poll_imap_connection_locked(
    db: AsyncSession, connection: MailboxConnection
) -> ImapPollSummary:
    if connection.provider != MailboxProviderName.IMAP:
        return ImapPollSummary(fetched=0, accepted=0, error="Not an IMAP connection.")
    if not connection.imap_host or not connection.imap_port or not connection.imap_password:
        return ImapPollSummary(fetched=0, accepted=0, error="IMAP connection is missing its credentials.")

    fetch_started = time.monotonic()
    try:
        last_uid = int(connection.imap_last_uid) if connection.imap_last_uid is not None else None
        fetched, max_uid_seen = await asyncio.to_thread(
            _fetch_new_sync,
            host=connection.imap_host,
            port=connection.imap_port,
            mailbox=connection.mailbox,
            password=connection.imap_password,
            since_uid=last_uid,
        )
    except (OSError, imaplib.IMAP4.error) as exc:
        connection.webhook_health = f"IMAP error: {exc}"[:255]
        await db.commit()
        return ImapPollSummary(fetched=0, accepted=0, error=str(exc))
    fetch_seconds = time.monotonic() - fetch_started
    if fetch_seconds > 5:
        # The actual IMAP round trip (connect/login/select/search/fetch)
        # taking this long -- as opposed to time spent waiting on the lock
        # above, which is logged separately -- points at the mailbox
        # provider's own response time, not anything in this app's control.
        # Deliberately worded to contain "IMAP poll" -- the same substring
        # already used for the lock-wait line and the final summary line,
        # since a real diagnostic session grepped for exactly that phrase
        # and a mismatched wording here would have made this line invisible.
        logger.info(
            "IMAP poll fetch for connection %s took %.1fs (connect+login+search+fetch)",
            connection.id,
            fetch_seconds,
        )

    accepted = 0
    successfully_ingested_uids: list[str] = []
    sessions_to_broadcast: list[str] = []

    for item in fetched:
        result: IngestResult = await ingest_message(db, connection=connection, message=item.message)
        _record_recent_message(
            connection.id,
            uid=item.uid,
            message=item.message,
            decision=result.event.decision.value,
            reason=result.event.decision_reason,
        )
        successfully_ingested_uids.append(item.uid)
        if (
            not result.already_processed
            and result.event.decision == ContributionDecision.ACCEPTED
            and result.event.session_id is not None
        ):
            accepted += 1
            sessions_to_broadcast.append(result.event.session_id)

    if max_uid_seen is not None:
        connection.imap_last_uid = max_uid_seen
    connection.last_sync_at = utcnow()
    connection.webhook_health = "ok"
    await db.commit()

    if successfully_ingested_uids:
        try:
            await asyncio.to_thread(
                _mark_seen_sync,
                host=connection.imap_host,
                port=connection.imap_port,
                mailbox=connection.mailbox,
                password=connection.imap_password,
                uids=successfully_ingested_uids,
            )
        except (OSError, imaplib.IMAP4.error):
            # Not fatal: the watermark has already advanced above, so this
            # is purely cosmetic for a human glancing at the mailbox --
            # ingestion tracking doesn't depend on the \Seen flag at all.
            logger.exception("Failed to mark %d IMAP message(s) as seen", len(successfully_ingested_uids))

    for session_id in sessions_to_broadcast:
        session = await db.get(Session, session_id)
        if session is not None:
            from app.api.v1.sessions import broadcast_session_update  # local import: avoids a circular import at module load time

            await broadcast_session_update(db, session)

    if fetched:
        # The only line in this module that fires on a normal successful
        # poll rather than an error -- lets a timestamp here be compared
        # against when a deposit email actually landed in the mailbox, to
        # tell whether a slow-to-appear contribution was a slow poll/IDLE
        # trigger vs. something downstream of ingestion.
        logger.info(
            "IMAP poll for connection %s: fetched=%d accepted=%d new_last_uid=%s",
            connection.id,
            len(fetched),
            accepted,
            connection.imap_last_uid,
        )

    return ImapPollSummary(fetched=len(fetched), accepted=accepted)


async def poll_all_imap_connections(db: AsyncSession) -> None:
    result = await db.execute(
        select(MailboxConnection).where(
            MailboxConnection.provider == MailboxProviderName.IMAP,
            MailboxConnection.status == ConnectionStatus.CONNECTED,
        )
    )
    for connection in result.scalars().all():
        try:
            await poll_imap_connection(db, connection)
        except Exception:  # noqa: BLE001 -- one bad mailbox must not stop the others
            logger.exception("Unhandled error polling IMAP connection %s", connection.id)
