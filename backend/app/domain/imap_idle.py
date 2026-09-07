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

# How long a single IDLE wait blocks before this task refreshes it on its
# own, comfortably under the ~29-minute RFC 2177-recommended renewal window
# so the connection never gets timed out server-side for staying idle too
# long. Whether idle_check returns because real mail arrived or because this
# elapsed, the next step is identical -- ask poll_imap_connection what's new.
_IDLE_TIMEOUT_SECONDS = 1400
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


def stop_watching(connection_id: str) -> None:
    task = _watchers.pop(connection_id, None)
    if task is not None:
        task.cancel()


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
    for connection in result.scalars().all():
        start_watching(connection.id)


def _connect_and_idle_sync(
    *, host: str, port: int, mailbox: str, password: str, timeout: int
) -> bool:
    """Blocking: connects, logs in, selects INBOX, and either idles for up
    to `timeout` seconds -- returning True the moment that ends, whether
    because the server actually pushed a notification or because the
    timeout simply elapsed, since the caller's next step is identical
    either way -- or, if this server doesn't support IDLE at all, returns
    False immediately so the caller falls back to a plain sleep-and-repoll
    cadence for this mailbox instead.
    """
    client = IMAPClient(host, port=port, ssl=True, timeout=15)
    try:
        client.login(mailbox, password)
        client.select_folder("INBOX")
        if not client.has_capability("IDLE"):
            return False
        client.idle()
        try:
            client.idle_check(timeout=timeout)
        finally:
            client.idle_done()
        return True
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass


async def _watch_connection(connection_id: str) -> None:
    """Long-running task, one per connected IMAP mailbox: waits for the
    server to say something changed (or a periodic timeout) and re-runs the
    existing poll_imap_connection pipeline. IDLE is only ever a trigger,
    never a second fetch path -- the actual parsing/ingestion stays exactly
    as already tested. Never needs manual recovery: any error just backs
    off and retries the connection from scratch on the next loop.
    """
    while True:
        idle_supported = False
        async with AsyncSessionLocal() as db:
            connection = await db.get(MailboxConnection, connection_id)
            if (
                connection is None
                or connection.status != ConnectionStatus.CONNECTED
                or connection.provider != MailboxProviderName.IMAP
            ):
                return  # revoked/deleted/gone -- this watcher's job is done

            try:
                await poll_imap_connection(db, connection)
            except Exception:  # noqa: BLE001 -- one bad cycle must not kill the watcher
                logger.exception("IMAP IDLE watcher catch-up poll failed for %s", connection_id)

            if not connection.imap_host or not connection.imap_port or not connection.imap_password:
                logger.warning("IMAP IDLE watcher for %s is missing credentials; stopping", connection_id)
                return

            try:
                idle_supported = await asyncio.to_thread(
                    _connect_and_idle_sync,
                    host=connection.imap_host,
                    port=connection.imap_port,
                    mailbox=connection.mailbox,
                    password=connection.imap_password,
                    timeout=_IDLE_TIMEOUT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 -- keep watching through transient errors
                logger.warning("IMAP IDLE connection lost for %s: %s", connection_id, exc)
                connection.webhook_health = f"Reconnecting after a lost IDLE connection: {exc}"[:255]
                await db.commit()
                await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
                continue

        if not idle_supported:
            await asyncio.sleep(_NO_IDLE_POLL_SECONDS)
        # else: loop straight back to the top for another catch-up poll.
