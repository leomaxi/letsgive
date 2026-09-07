import email.utils
import imaplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.imap_polling import _fetch_new_sync, _parse_message
from app.domain.imap_provider import ImapAuthError, connect_and_get_baseline_uid, guess_imap_host
from tests.conftest import CapturingNotifier
from tests.helpers import (
    add_active_member,
    approve_session,
    create_org,
    create_parser_profile,
    create_session,
    enable_mfa,
    register_and_login,
)


async def _owner_org(client: AsyncClient, suffix: str) -> tuple[str, dict]:
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")
    return owner_token, org


def _build_raw_email(
    *,
    sender: str = "notifications@fakebank.com",
    subject: str = "Deposit received",
    body: str = "You received $42.50 CAD. Funds deposited to your account.",
    message_id: str | None = "<abc123@fakebank.com>",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "deposits@church.org"
    msg["Subject"] = subject
    # A few seconds in the future, not exactly "now" -- a session's watermark
    # is set (with microsecond precision) at approval time, moments before
    # this is built, but the RFC 5322 Date header format truncates to whole
    # seconds. Without the buffer, a message dated the same wall-clock second
    # as the watermark can round-trip to a timestamp a fraction of a second
    # *before* it, and get excluded as EXCLUDED_TIME_WINDOW -- a test timing
    # artifact, not anything a real bank notification (arriving seconds to
    # minutes later) would ever hit.
    msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc) + timedelta(seconds=5))
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return bytes(msg)


def test_guess_imap_host_recognizes_common_providers_and_falls_back_to_none():
    assert guess_imap_host("deposits@gmail.com") == ("imap.gmail.com", 993)
    assert guess_imap_host("deposits@outlook.com") == ("outlook.office365.com", 993)
    assert guess_imap_host("deposits@yahoo.com") == ("imap.mail.yahoo.com", 993)
    assert guess_imap_host("deposits@my-custom-church-domain.org") is None


async def test_connect_and_get_baseline_uid_succeeds_and_reads_uidnext():
    imap_instance = MagicMock()
    imap_instance.login.return_value = ("OK", [b"done"])
    imap_instance.status.return_value = ("OK", [b"INBOX (UIDNEXT 43)"])
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        baseline = await connect_and_get_baseline_uid(
            host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="app-pw"
        )
    imap_instance.login.assert_called_once_with("a@gmail.com", "app-pw")
    imap_instance.logout.assert_called_once()
    # UIDNEXT 43 means everything up to and including UID 42 already exists
    # at connect time -- the baseline is "not new", not "the next message".
    assert baseline == 42


async def test_connect_and_get_baseline_uid_wraps_auth_failure_in_a_readable_message():
    imap_instance = MagicMock()
    imap_instance.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        try:
            await connect_and_get_baseline_uid(
                host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="wrong"
            )
            raise AssertionError("expected ImapAuthError")
        except ImapAuthError as exc:
            assert "app-specific password" in str(exc)


async def test_connect_and_get_baseline_uid_wraps_a_network_error_after_login_too():
    # A real production bug: login succeeded, but the network hiccuped on
    # the very next call (STATUS ... UIDNEXT). The old code only wrapped
    # imaplib.IMAP4.error there, so a raw OSError (timeout, connection
    # reset) propagated all the way out of the API endpoint unhandled -- a
    # bare 500 with no useful message, instead of the same clean,
    # user-facing error a login failure already gets.
    imap_instance = MagicMock()
    imap_instance.login.return_value = ("OK", [b"done"])
    imap_instance.status.side_effect = ConnectionResetError("connection reset by peer")
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        try:
            await connect_and_get_baseline_uid(
                host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="app-pw"
            )
            raise AssertionError("expected ImapAuthError")
        except ImapAuthError as exc:
            assert "imap.gmail.com" in str(exc)


def test_parse_message_extracts_sender_subject_body_and_message_id():
    fetched = _parse_message(b"12", _build_raw_email())
    assert fetched.uid == "12"
    assert fetched.message.sender == "notifications@fakebank.com"
    assert fetched.message.subject == "Deposit received"
    assert "42.50" in fetched.message.body
    assert fetched.message.provider_message_id == "<abc123@fakebank.com>"


def test_parse_message_falls_back_to_a_stable_hash_when_message_id_is_missing():
    fetched = _parse_message(b"13", _build_raw_email(message_id=None))
    assert fetched.message.provider_message_id.startswith("imap-")
    # Deterministic, not random -- reprocessing the identical bytes (e.g. a
    # retried poll after a crash before the \Seen flag was set) must produce
    # the same id, or ingest_message's dedup constraint couldn't catch it.
    again = _parse_message(b"13", _build_raw_email(message_id=None))
    assert again.message.provider_message_id == fetched.message.provider_message_id


def test_parse_message_extracts_text_from_an_html_only_email():
    # A real deposit notification (Interac's own "Funds Deposited" template)
    # was missed in production: the email was HTML-only, and _extract_body
    # used to only ever look at text/plain, silently returning "" and
    # losing the amount/keywords entirely. This is that email's actual shape.
    msg = EmailMessage()
    msg["From"] = "LEONARD MAXIMUS MENSAH <notify@payments.interac.ca>"
    msg["To"] = "methodistchurchstjohns@gmail.com"
    msg["Subject"] = "Interac e-Transfer: You've received $1.00 and it has been automatically deposited."
    msg["Message-ID"] = "<interac-1@payments.interac.ca>"
    msg.add_header("Content-Type", "text/html", charset="utf-8")
    msg.set_payload(
        "<html><body><table><tr><td>Funds Deposited!</td></tr>"
        "<tr><td>$1.00</td></tr><tr><td>Amount:</td><td>$1.00 (CAD)</td></tr>"
        "</table></body></html>",
        charset="utf-8",
    )

    fetched = _parse_message(b"1", bytes(msg))
    assert "Funds Deposited" in fetched.message.body
    assert "$1.00" in fetched.message.body
    # Tags must become whitespace, not nothing -- otherwise adjacent cells
    # like "<td>$1.00</td><td>(CAD)</td>" would fuse into "$1.00(CAD)".
    assert "$1.00 (CAD)" in fetched.message.body
    assert "<td>" not in fetched.message.body


def test_parse_message_prefers_html_content_over_a_bare_plaintext_stub():
    # Multipart/alternative with a plain-text part that exists but omits the
    # real content -- common for ESP-generated transactional email, and a
    # second way the same bug could bite: finding *a* text/plain part isn't
    # enough if that part is a stub. Both parts get collected, so whichever
    # one has the real text is found either way.
    msg = EmailMessage()
    msg["From"] = "notify@payments.interac.ca"
    msg["To"] = "methodistchurchstjohns@gmail.com"
    msg["Subject"] = "Deposit notification"
    msg["Message-ID"] = "<interac-2@payments.interac.ca>"
    msg.set_content("View this email in HTML to see your transaction details.")
    msg.add_alternative(
        "<html><body>Funds Deposited! Amount: $75.00 (CAD)</body></html>", subtype="html"
    )

    fetched = _parse_message(b"2", bytes(msg))
    assert "Funds Deposited" in fetched.message.body
    assert "$75.00" in fetched.message.body


def test_fetch_new_sync_does_a_full_sweep_when_no_watermark_exists_yet():
    # A real deposit was missed in production because the old design used
    # "search UNSEEN" as its only tracking mechanism: the user read the
    # notification email in their own phone's mail app, which marked it
    # \Seen, and the poller (which only ever looked for UNSEEN mail) never
    # found it again. since_uid=None models a connection that predates the
    # UID watermark (MailboxConnection.imap_last_uid) existing at all -- it
    # must sweep the whole mailbox with "ALL", not "UNSEEN", so an
    # already-read message still gets picked up.
    mock = MagicMock()
    mock.uid.side_effect = lambda command, *args: (
        ("OK", [b"1 2"]) if command == "search" else ("OK", [(b"1 (BODY[] {0}", _build_raw_email()), b")"])
    )

    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=mock):
        fetched, max_uid_seen = _fetch_new_sync(
            host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="pw", since_uid=None
        )

    mock.uid.assert_any_call("search", None, "ALL")
    assert max_uid_seen == 2
    assert len(fetched) == 2


def test_fetch_new_sync_only_returns_uids_strictly_greater_than_the_watermark():
    # IMAP's "N:*" range has a real quirk (RFC 3501): if N is past every
    # real UID, some servers return the *last* message instead of nothing.
    # Searching inclusive of since_uid and filtering it back out in Python
    # (rather than trusting "since_uid+1:*") guards against that.
    mock = MagicMock()
    mock.uid.side_effect = lambda command, *args: (
        ("OK", [b"5"]) if command == "search" else ("OK", [(b"5 (BODY[] {0}", _build_raw_email()), b")"])
    )

    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=mock):
        fetched, max_uid_seen = _fetch_new_sync(
            host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="pw", since_uid=5
        )

    mock.uid.assert_any_call("search", None, "UID 5:*")
    assert fetched == []
    assert max_uid_seen is None


async def test_create_imap_connection_succeeds_with_an_auto_guessed_host(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_org(client, "imapok")
    imap_instance = MagicMock()
    imap_instance.login.return_value = ("OK", [b"done"])
    imap_instance.status.return_value = ("OK", [b"INBOX (UIDNEXT 1)"])
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections",
            json={"provider": "imap", "mailbox": "deposits@gmail.com", "imap_password": "app-pw"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "connected"
    assert body["imap_host"] == "imap.gmail.com"
    assert body["imap_port"] == 993
    assert "imap_password" not in body


async def test_create_imap_connection_rejects_bad_credentials_and_persists_nothing(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_org(client, "imapbad")
    imap_instance = MagicMock()
    imap_instance.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections",
            json={"provider": "imap", "mailbox": "deposits@gmail.com", "imap_password": "wrong"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 400
    assert "app-specific password" in resp.json()["detail"]

    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.json() == []


async def test_create_imap_connection_on_an_unrecognized_domain_requires_a_host(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_org(client, "imapunknown")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections",
        json={"provider": "imap", "mailbox": "deposits@mychurch.org", "imap_password": "app-pw"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "IMAP server" in resp.json()["detail"]


async def test_check_now_polls_ingests_and_marks_the_message_seen(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    owner_token, org = await _owner_org(client, "imappoll")
    await add_active_member(
        client, db_session, owner_token, org["id"], "finance-imappoll@example.org", "finance", needs_mfa=True
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-imappoll@example.org", "media"
    )

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
    connection = resp.json()

    await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@fakebank.com"]
    )
    session = await create_session(
        client, owner_token, org["id"], mailbox_connection_id=connection["id"]
    )
    authorized = await approve_session(client, notifier, owner_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    def uid_command(command: str, *args):
        if command == "search":
            return ("OK", [b"1"])
        if command == "fetch":
            return ("OK", [(b"1 (BODY[] {0}", _build_raw_email()), b")"])
        if command == "store":
            return ("OK", [b"done"])
        raise AssertionError(f"unexpected UID command: {command}")

    poll_mock = MagicMock()
    poll_mock.login.return_value = ("OK", [b"done"])
    poll_mock.uid.side_effect = uid_command

    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=poll_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections/{connection['id']}/check-now",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"fetched": 1, "accepted": 1, "error": None}
    poll_mock.uid.assert_any_call("store", "1", "+FLAGS", "\\Seen")

    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert resp.json()["contribution_count"] == 1

    # Idempotent: polling again doesn't even refetch UID 1 (the watermark
    # already advanced past it), and even if it did, ingest_message's dedup
    # guarantee (shared with every other provider) would still stop it from
    # being double-counted.
    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=poll_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections/{connection['id']}/check-now",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 0

    # The raw fetched-message log shows what actually happened, independent
    # of the parser's decision -- this is the diagnostic surface for "why
    # wasn't my deposit counted" without guessing blind.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/recent-messages",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    log = resp.json()
    assert len(log) == 1
    assert log[0]["sender"] == "notifications@fakebank.com"
    assert log[0]["subject"] == "Deposit received"
    assert "42.50" in log[0]["body_snippet"]
    assert log[0]["decision"] == "accepted"

    # Raw sender/subject/body content is more sensitive than the sanitized
    # ledger data other list endpoints expose -- stays Owner/Finance-only,
    # not opened to every active member the way audit-logs/reconciliation/
    # connections were.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/recent-messages",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_broadcast_runs_before_marking_the_message_seen(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    # Real production measurement: marking \Seen is its own separate IMAP
    # connect/login/select/store round trip, and on a slow connection it took
    # 25-35s on its own -- time that used to run *before* the WebSocket
    # broadcast that makes the operator console and public display actually
    # update, needlessly delaying the one thing that has to feel instant for
    # a purely cosmetic mailbox flag a human might never even look at.
    owner_token, org = await _owner_org(client, "imapbroadcastorder")
    await add_active_member(
        client, db_session, owner_token, org["id"], "finance-imapbroadcastorder@example.org",
        "finance", needs_mfa=True,
    )

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
    connection = resp.json()

    await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@fakebank.com"]
    )
    session = await create_session(
        client, owner_token, org["id"], mailbox_connection_id=connection["id"]
    )
    authorized = await approve_session(client, notifier, owner_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    order: list[str] = []

    def uid_command(command: str, *args):
        if command == "search":
            return ("OK", [b"1"])
        if command == "fetch":
            return ("OK", [(b"1 (BODY[] {0}", _build_raw_email()), b")"])
        if command == "store":
            order.append("mark_seen")
            return ("OK", [b"done"])
        raise AssertionError(f"unexpected UID command: {command}")

    poll_mock = MagicMock()
    poll_mock.login.return_value = ("OK", [b"done"])
    poll_mock.uid.side_effect = uid_command

    async def fake_broadcast(db, session):
        order.append("broadcast")

    with (
        patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=poll_mock),
        patch("app.api.v1.sessions.broadcast_session_update", side_effect=fake_broadcast),
    ):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections/{connection['id']}/check-now",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"fetched": 1, "accepted": 1, "error": None}
    assert order == ["broadcast", "mark_seen"]
