import asyncio
import time
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.mailbox_connection import MailboxConnection
from app.domain import imap_idle
from app.domain.imap_idle import _connect_and_idle_sync
from app.domain.imap_polling import poll_imap_connection
from tests.helpers import create_org, enable_mfa, register_and_login


async def _owner_org(client: AsyncClient, suffix: str) -> tuple[str, dict]:
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")
    return owner_token, org


# --- poll_imap_connection's per-connection lock -----------------------------


async def test_poll_imap_connection_serializes_concurrent_calls_for_the_same_connection(
    client: AsyncClient, db_session: AsyncSession
):
    # Real production bug: the manual "check now" button and a background
    # trigger (the old poll loop, now the IDLE watcher) could both call
    # poll_imap_connection for the same connection at once, racing the same
    # IMAP session/watermark update -- observed in production as a raw 500
    # on the manual click while the same message quietly succeeded via the
    # other path moments later.
    owner_token, org = await _owner_org(client, "lockrace")
    creation_mock = MagicMock()
    creation_mock.login.return_value = ("OK", [b"done"])
    creation_mock.status.return_value = ("OK", [b"INBOX (UIDNEXT 1)"])
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=creation_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections",
            json={"provider": "imap", "mailbox": "deposits@gmail.com", "imap_password": "app-pw"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 201, resp.text
    connection_id = resp.json()["id"]

    result = await db_session.execute(
        select(MailboxConnection).where(MailboxConnection.id == connection_id)
    )
    connection = result.scalar_one()

    order: list[str] = []

    def fake_fetch_new_sync(**kwargs):
        order.append("start")
        time.sleep(0.05)  # runs in the real thread pool via asyncio.to_thread
        order.append("end")
        return [], None

    with patch("app.domain.imap_polling._fetch_new_sync", side_effect=fake_fetch_new_sync):
        await asyncio.gather(
            poll_imap_connection(db_session, connection),
            poll_imap_connection(db_session, connection),
        )

    # Without the lock, both calls' "start" would land back-to-back before
    # either "end" -- ["start", "start", "end", "end"] or similar interleave.
    assert order == ["start", "end", "start", "end"]


# --- _connect_and_idle_sync's IDLE-vs-fallback branch ------------------------


def test_connect_and_idle_sync_reports_a_real_push_when_the_server_sends_one():
    client_mock = MagicMock()
    client_mock.has_capability.return_value = True
    client_mock.idle_check.return_value = [(b"3", b"EXISTS")]
    with patch("app.domain.imap_idle.IMAPClient", return_value=client_mock):
        result = _connect_and_idle_sync(
            connection_id="conn-push", host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="pw", timeout=5
        )

    assert result == "pushed"
    # Deregistered from _active_clients once the wait ends -- otherwise a
    # later _force_disconnect for this connection id would try to shut down
    # a socket that's already been logged out and closed.
    assert "conn-push" not in imap_idle._active_clients
    client_mock.login.assert_called_once_with("a@gmail.com", "pw")
    client_mock.select_folder.assert_called_once_with("INBOX")
    client_mock.idle.assert_called_once()
    client_mock.idle_check.assert_called_once_with(timeout=5)
    client_mock.idle_done.assert_called_once()
    client_mock.logout.assert_called_once()


def test_connect_and_idle_sync_reports_a_timeout_when_the_server_sends_nothing():
    client_mock = MagicMock()
    client_mock.has_capability.return_value = True
    client_mock.idle_check.return_value = []
    with patch("app.domain.imap_idle.IMAPClient", return_value=client_mock):
        result = _connect_and_idle_sync(
            connection_id="conn-timeout", host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="pw", timeout=5
        )

    assert result == "timed_out"


def test_connect_and_idle_sync_falls_back_when_the_server_lacks_idle():
    # Rare but real for some smaller/self-hosted IMAP servers -- must not
    # raise or hang waiting on a command the server never promised to
    # support; the caller (the watcher loop) falls back to plain polling.
    client_mock = MagicMock()
    client_mock.has_capability.return_value = False
    with patch("app.domain.imap_idle.IMAPClient", return_value=client_mock):
        result = _connect_and_idle_sync(
            connection_id="conn-nosupport",
            host="imap.example.com",
            port=993,
            mailbox="a@example.com",
            password="pw",
            timeout=5,
        )

    assert result == "not_supported"
    client_mock.idle.assert_not_called()
    client_mock.idle_check.assert_not_called()
    client_mock.logout.assert_called_once()


def test_connect_and_idle_sync_registers_itself_for_force_disconnect_while_idling():
    # _force_disconnect (called by stop_watching/stop_all_watchers, e.g. on
    # process shutdown) needs to find the live client while it's blocked in
    # idle_check -- registered right before that call, deregistered right
    # after, so a stray _force_disconnect call outside that window is a
    # harmless no-op rather than shutting down a socket someone else is
    # using.
    seen_during_idle = {}

    client_mock = MagicMock()
    client_mock.has_capability.return_value = True

    def fake_idle_check(timeout):
        seen_during_idle["client"] = imap_idle._active_clients.get("conn-registered")
        return []

    client_mock.idle_check.side_effect = fake_idle_check
    with patch("app.domain.imap_idle.IMAPClient", return_value=client_mock):
        _connect_and_idle_sync(
            connection_id="conn-registered",
            host="imap.gmail.com",
            port=993,
            mailbox="a@gmail.com",
            password="pw",
            timeout=5,
        )

    assert seen_during_idle["client"] is client_mock
    assert "conn-registered" not in imap_idle._active_clients


def test_force_disconnect_shuts_down_a_registered_clients_socket():
    client_mock = MagicMock()
    imap_idle._active_clients["conn-force"] = client_mock
    try:
        imap_idle._force_disconnect("conn-force")
    finally:
        imap_idle._active_clients.pop("conn-force", None)

    client_mock.shutdown.assert_called_once()


def test_force_disconnect_for_an_unregistered_connection_is_a_safe_no_op():
    imap_idle._force_disconnect("no-such-connection")


def test_force_disconnect_swallows_errors_from_an_already_closed_socket():
    client_mock = MagicMock()
    client_mock.shutdown.side_effect = OSError("socket already closed")
    imap_idle._active_clients["conn-already-closed"] = client_mock
    try:
        imap_idle._force_disconnect("conn-already-closed")  # must not raise
    finally:
        imap_idle._active_clients.pop("conn-already-closed", None)


# --- watcher task registry ---------------------------------------------------


async def test_start_watching_is_a_no_op_under_pytest():
    # Regression test for a real bug found this session: create_connection
    # called start_watching() with no guard, which spawned a real
    # background task opening its own AsyncSessionLocal() against the
    # module-level (non-test) database engine -- crashing with "no such
    # table" the moment any test created an IMAP connection, since the
    # test suite's actual data lives in a per-test in-memory SQLite
    # database reached only through the get_db dependency override.
    imap_idle.start_watching("some-connection-id-not-in-the-registry")
    assert "some-connection-id-not-in-the-registry" not in imap_idle._watchers


async def test_stop_watching_cancels_and_removes_a_registered_task():
    async def _never_finishes():
        await asyncio.sleep(100)

    task = asyncio.create_task(_never_finishes())
    imap_idle._watchers["fake-connection-id"] = task
    imap_idle.stop_watching("fake-connection-id")

    assert "fake-connection-id" not in imap_idle._watchers
    await asyncio.sleep(0)  # let the cancellation actually land
    assert task.cancelled()


async def test_stop_watching_force_disconnects_a_mid_idle_client():
    # Real production bug: cancelling this task alone doesn't stop it -- the
    # actual blocking work happens in a separate OS thread (asyncio.to_thread)
    # that asyncio's cooperative cancellation can't preempt. A `systemctl
    # restart` while a mailbox was mid-IDLE-wait left the process stuck in
    # "Waiting for background tasks to complete" for the full ~90s systemd
    # stop timeout before being SIGKILLed. stop_watching must also force-close
    # the socket so the blocked thread actually returns promptly.
    async def _never_finishes():
        await asyncio.sleep(100)

    client_mock = MagicMock()
    task = asyncio.create_task(_never_finishes())
    imap_idle._watchers["fake-mid-idle-connection"] = task
    imap_idle._active_clients["fake-mid-idle-connection"] = client_mock

    imap_idle.stop_watching("fake-mid-idle-connection")

    client_mock.shutdown.assert_called_once()


async def test_stop_watching_an_unknown_connection_is_a_safe_no_op():
    imap_idle.stop_watching("this-connection-was-never-registered")
