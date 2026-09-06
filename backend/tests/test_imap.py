import email.utils
import imaplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.imap_polling import _parse_message
from app.domain.imap_provider import ImapAuthError, guess_imap_host, verify_imap_login
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


async def test_verify_imap_login_succeeds_on_ok_response():
    imap_instance = MagicMock()
    imap_instance.login.return_value = ("OK", [b"done"])
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        await verify_imap_login(host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="app-pw")
    imap_instance.login.assert_called_once_with("a@gmail.com", "app-pw")
    imap_instance.logout.assert_called_once()


async def test_verify_imap_login_wraps_auth_failure_in_a_readable_message():
    imap_instance = MagicMock()
    imap_instance.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED")
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=imap_instance):
        try:
            await verify_imap_login(host="imap.gmail.com", port=993, mailbox="a@gmail.com", password="wrong")
            raise AssertionError("expected ImapAuthError")
        except ImapAuthError as exc:
            assert "app-specific password" in str(exc)


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


async def test_create_imap_connection_succeeds_with_an_auto_guessed_host(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token, org = await _owner_org(client, "imapok")
    imap_instance = MagicMock()
    imap_instance.login.return_value = ("OK", [b"done"])
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

    creation_mock = MagicMock()
    creation_mock.login.return_value = ("OK", [b"done"])
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

    poll_mock = MagicMock()
    poll_mock.login.return_value = ("OK", [b"done"])
    poll_mock.search.return_value = ("OK", [b"1"])
    poll_mock.fetch.return_value = ("OK", [(b"1 (BODY[] {0}", _build_raw_email()), b")"])

    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=poll_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections/{connection['id']}/check-now",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"fetched": 1, "accepted": 1, "error": None}
    poll_mock.store.assert_called_once_with("1", "+FLAGS", "\\Seen")

    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert resp.json()["contribution_count"] == 1

    # Idempotent: polling again with the *same* message (now already
    # ingested) must not double-count it, matching ingest_message's dedup
    # guarantee for every other provider.
    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=poll_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections/{connection['id']}/check-now",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 0
