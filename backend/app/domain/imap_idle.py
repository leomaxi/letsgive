import asyncio
import logging
import sys

from imapclient import IMAPClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.mailbox_connection import ConnectionStatus, MailboxConnection, MailboxProviderName
from app.db.session import AsyncSessionLocal
from app.domain.imap_polling import poll_imap_connection

logger = logging.getLogger("letsgive.imap.idle")
# uvicorn's default logging config doesn't attach a handler to arbitrary app
# loggers, so INFO messages would silently vanish in journalctl -- same fix
# already used in app/domain/notifications.py and app/domain/imap_polling.py.
# This logger is a *child* of "letsgive.imap" (imap_polling.py's logger name)
# but still needs its own handler here: imap_polling.py's own fix sets
# propagate=False on its logger, which only stops that logger's own records
# from bubbling further up -- it does nothing for a sibling/child logger's
# records, which still need somewhere to go.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# How long a single IDLE wait blocks before this task verifies the mailbox
# again. IDLE is still the fast path when the provider reliably pushes a new
# mail notification, but real Gmail/IMAP deployments have shown that "the
# email is visible in the inbox" can happen without our IDLE wait waking
# promptly. Keeping this short bounds the user-visible deposit delay even
# when the provider's push path is flaky.
_IDLE_TIMEOUT_SECONDS = 20
# Cadence for a mailbox whose provider doesn't support IDLE at all (rare,
# but real for some smaller/self-hosted providers) -- still much tighter
# than app/main.py's slower fallback loop, since this is the per-mailbox
# fast path, just without a real push mechanism underneath.
_NO_IDLE_POLL_SECONDS = 20
# Backoff after a connection error before retrying -- avoids hammering a
# mailbox that's actively rejecting connections (e.g. a revoked app
# password), while still recovering on its own once the problem clears.
_ERROR_BACKOFF_SECONDS = 30

# One task per watched connection. Module-level and in-process, same
# single-server assumption as app/domain/realtime.py and the poll loops in
# app/main.py -- a horizontally-scaled deployment would need to run this on
# exactly one worker, or move it to a real task queue.
_watchers: dict[str, asyncio.Task] = {}

# The IMAPClient currently blocked in idle_check() for each connection, if
# any -- registered/unregistered by _connect_and_idle_sync, which runs in a
# worker thread (asyncio.to_thread). Plain dict set/pop item access is
# already how _watchers above is shared across the event loop without an
# explicit lock; the GIL makes a single dict item assignment/deletion atomic
# enough for that, and this follows the same pattern. Lets stop_watching /
# stop_all_watchers force-unblock a socket read that could otherwise block
# for up to _IDLE_TIMEOUT_SECONDS -- see _force_disconnect.
_active_clients: dict[str, IMAPClient] = {}


def _force_disconnect(connection_id: str) -> None:
    """Force-closes the socket of a connection's in-progress IDLE wait, if
    any, so a cancelled watcher task actually stops promptly instead of
    asyncio.to_thread's underlying worker thread running the blocking
    idle_check() call to completion regardless of the cancellation --
    asyncio can't interrupt a real OS thread blocked in a socket read.

    This isn't a hypothetical: without it, a `systemctl restart` issued
    while a mailbox was mid-IDLE-wait made uvicorn's graceful shutdown sit
    in "Waiting for background tasks to complete" until systemd's stop
    timeout elapsed and SIGKILLed the process -- observed in production as
    two ~90-second stuck restarts back to back. IMAPClient.shutdown() calls
    the underlying socket's shutdown(SHUT_RDWR), which is safe to call from
    a different thread than the one blocked reading it -- a standard way to
    unblock a stuck socket read.
    """
    client = _active_clients.get(connection_id)
    if client is not None:
        try:
            client.shutdown()
        except Exception:  # noqa: BLE001 -- best-effort; the read it unblocks handles the rest
            pass


def start_watching(connection_id: str) -> None:
    """No-op if already watching this connection -- safe to call from
    multiple lifecycle points (create, and the startup sweep) without
    double-starting a watcher.

    Also a no-op under pytest: this task would open its own real
    AsyncSessionLocal() session against the module-level engine, not the
    per-test in-memory SQLite database reached only through the
    dependency-injected get_db override -- same reasoning as the poll loops
    in app/main.py being skipped there. Guarded here, not just at the
    lifespan call site, so a request-triggered call (create_connection)
    can't spawn a real stray background task during a test run either.
    """
    if "pytest" in sys.modules:
        return
    existing = _watchers.get(connection_id)
    if existing is not None and not existing.done():
        return
    _watchers[connection_id] = asyncio.create_task(_watch_connection(connection_id))
    logger.info("Started IMAP IDLE watcher for connection %s", connection_id)


def stop_watching(connection_id: str) -> None:
    task = _watchers.pop(connection_id, None)
    if task is not None:
        task.cancel()
        _force_disconnect(connection_id)
        logger.info("Stopped IMAP IDLE watcher for connection %s", connection_id)


def stop_all_watchers() -> None:
    for connection_id in list(_watchers):
        stop_watching(connection_id)


async def start_all_watchers(db: AsyncSession) -> None:
    """Called once at process startup (app/main.py's lifespan) -- starts a
    watcher for every mailbox that was already connected before this
    process started, mirroring poll_all_imap_connections's existing query.
    """
    result = await db.execute(
        select(MailboxConnection).where(
            MailboxConnection.provider == MailboxProviderName.IMAP,
            MailboxConnection.status == ConnectionStatus.CONNECTED,
        )
    )
    connections = result.scalars().all()
    for connection in connections:
        start_watching(connection.id)
    logger.info("IMAP IDLE startup sweep: watching %d connection(s)", len(connections))


def _connect_and_idle_sync(
    *, connection_id: str, host: str, port: int, mailbox: str, password: str, timeout: int
) -> str:
    """Blocking: connects, logs in, selects INBOX, and either idles for up
    to `timeout` seconds or, if this server doesn't support IDLE at all,
    returns immediately so the caller falls back to a plain sleep-and-repoll
    cadence for this mailbox instead.

    Returns one of:
    - "not_supported": no IDLE capability; caller falls back to polling.
    - "pushed": the server sent something (almost always new mail) before
      `timeout` elapsed.
    - "timed_out": `timeout` elapsed with no server activity at all.

    The caller's next step (a catch-up poll, then re-idle) is identical for
    "pushed" and "timed_out" either way, but the distinction is logged --
    it's the only way to tell from the logs whether real push notifications
    are actually arriving or this mailbox is quietly relying on periodic
    self-verification instead.

    Registers itself in _active_clients for the duration of the IDLE wait so
    _force_disconnect (called from the event loop thread, e.g. on shutdown)
    can force-close the socket to unblock this thread promptly instead of
    leaving it running for up to `timeout` seconds regardless of
    cancellation.
    """
    client = IMAPClient(host, port=port, ssl=True, timeout=15)
    try:
        client.login(mailbox, password)
        client.select_folder("INBOX")
        if not client.has_capability("IDLE"):
            return "not_supported"
        client.idle()
        _active_clients[connection_id] = client
        try:
            responses = client.idle_check(timeout=timeout)
        finally:
            _active_clients.pop(connection_id, None)
            client.idle_done()
        return "pushed" if responses else "timed_out"
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


async def _watch_connection(connection_id: str) -> None:
    """Long-running task, one per connected IMAP mailbox. Never needs manual
    recovery: this outer loop catches literally anything an iteration could
    raise and just retries from scratch, matching the same "one bad cycle
    must not kill the loop" resilience the poll loops in app/main.py
    already have -- without it, an exception escaping a DB session's
    implicit close (e.g. after an earlier query failed and left the
    session needing a rollback) would silently end this task for good, no
    more real-time detection for this mailbox until the next full process
    restart, with nothing in the logs pointing at why.
    """
    while True:
        try:
            if await _watch_connection_iteration(connection_id):
                return
        except Exception:  # noqa: BLE001
            logger.exception("IMAP IDLE watcher iteration failed for %s", connection_id)
            await asyncio.sleep(_ERROR_BACKOFF_SECONDS)


async def _watch_connection_iteration(connection_id: str) -> bool:
    """One catch-up-poll-then-IDLE-wait cycle. Returns True if this watcher
    should stop entirely (the connection was revoked/deleted/lost its
    credentials), False to keep looping. IDLE is only ever a trigger, never
    a second fetch path -- the actual parsing/ingestion stays exactly as
    already tested in poll_imap_connection.
    """
    async with AsyncSessionLocal() as db:
        connection = await db.get(MailboxConnection, connection_id)
        if (
            connection is None
            or connection.status != ConnectionStatus.CONNECTED
            or connection.provider != MailboxProviderName.IMAP
        ):
            return True  # revoked/deleted/gone -- this watcher's job is done

        try:
            await poll_imap_connection(db, connection)
        except Exception:  # noqa: BLE001 -- one bad cycle must not stop watching
            logger.exception("IMAP IDLE watcher catch-up poll failed for %s", connection_id)
            # Without this, a session left in a failed-transaction state by
            # the exception above could raise *again* on implicit
            # close/commit when this `async with` block exits below --
            # exactly the real production failure that motivated splitting
            # this function out of the old single-session version of it.
            await db.rollback()

        if not connection.imap_host or not connection.imap_port or not connection.imap_password:
            logger.warning("IMAP IDLE watcher for %s is missing credentials; stopping", connection_id)
            return True

        host, port, mailbox, password = (
            connection.imap_host,
            connection.imap_port,
            connection.mailbox,
            connection.imap_password,
        )

    # The session above is already closed -- the long IDLE wait below
    # deliberately holds no database connection open across it. A MySQL
    # server's own idle-connection timeout (or a proxy/pooler in front of
    # it) can kill a connection that just sits open-but-unused for too long;
    # the next query on it then fails with a "server has gone away"-style
    # error instead of a clean reconnect -- a real production failure this
    # split fixes.
    try:
        idle_outcome = await asyncio.to_thread(
            _connect_and_idle_sync,
            connection_id=connection_id,
            host=host,
            port=port,
            mailbox=mailbox,
            password=password,
            timeout=_IDLE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- keep watching through transient errors
        logger.warning("IMAP IDLE connection lost for %s: %s", connection_id, exc)
        async with AsyncSessionLocal() as db:
            connection = await db.get(MailboxConnection, connection_id)
            if connection is not None:
                connection.webhook_health = f"Reconnecting after a lost IDLE connection: {exc}"[:255]
                await db.commit()
        await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
        return False

    # The one line that actually answers "is IDLE working" -- "pushed" means
    # the server itself notified us before the verification timeout; a string
    # of "timed_out" entries for a mailbox that should be receiving mail
    # means real push notifications aren't arriving even though IDLE was
    # negotiated, and new mail is being caught by the next self-verification
    # poll or the slower app/main.py fallback loop.
    logger.info("IMAP IDLE wait for %s ended: %s", connection_id, idle_outcome)

    if idle_outcome == "not_supported":
        await asyncio.sleep(_NO_IDLE_POLL_SECONDS)
    return False
