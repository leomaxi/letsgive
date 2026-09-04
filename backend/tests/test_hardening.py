from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.mailbox_connection import MailboxConnection
from app.domain.rate_limit import login_ip_rate_limiter, login_rate_limiter
from tests.conftest import CapturingNotifier
from tests.helpers import (
    add_active_member,
    approve_session,
    create_fake_connection,
    create_org,
    create_parser_profile,
    create_session,
    deliver_webhook,
    enable_mfa,
    register_and_login,
)

SENDER = "notifications@fakebank.com"


async def test_login_rate_limited_after_repeated_failures(client: AsyncClient, db_session: AsyncSession):
    email = "ratelimited@example.org"
    await register_and_login(client, email)
    login_rate_limiter.reset(f"login:{email.lower()}")

    last_resp = None
    for _ in range(10):
        last_resp = await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
        assert last_resp.status_code == 401

    resp = await client.post("/v1/auth/login", json={"email": email, "password": "wrong-password"})
    assert resp.status_code == 429

    # Even the *correct* password is now rejected until the window clears --
    # that's the point, an attacker who eventually guesses right shouldn't win.
    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 429

    login_rate_limiter.reset(f"login:{email.lower()}")


async def test_successful_login_resets_rate_limit_counter(
    client: AsyncClient, db_session: AsyncSession
):
    email = "resetcounter@example.org"
    await register_and_login(client, email)
    key = f"login:{email.lower()}"
    login_rate_limiter.reset(key)

    for _ in range(5):
        resp = await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
        assert resp.status_code == 401

    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    assert resp.status_code == 200

    # Counter cleared by the success, so the next few failures don't
    # immediately trip the limiter left over from before.
    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": "wrong-password"}
    )
    assert resp.status_code == 401

    login_rate_limiter.reset(key)


async def test_login_rate_limited_by_ip_across_different_emails(
    client: AsyncClient, db_session: AsyncSession
):
    """A credential-stuffing IP tries many different accounts, none enough
    times to trip its own email-keyed counter -- the shared IP-keyed one
    should catch that pattern instead."""
    emails = [f"ip-flood-{i}@example.org" for i in range(31)]
    for email in emails:
        resp = await client.post(
            "/v1/auth/register",
            json={"email": email, "password": "correct-horse-battery", "full_name": "Test User"},
        )
        assert resp.status_code == 201, resp.text
    login_ip_rate_limiter.clear_all()

    last_resp = None
    for email in emails[:30]:
        last_resp = await client.post(
            "/v1/auth/login", json={"email": email, "password": "wrong-password"}
        )
        assert last_resp.status_code == 401

    # The 31st distinct email's own counter is still completely fresh --
    # it trips the shared IP-keyed limiter instead.
    resp = await client.post(
        "/v1/auth/login", json={"email": emails[30], "password": "wrong-password"}
    )
    assert resp.status_code == 429

    login_ip_rate_limiter.clear_all()


async def _live_session_with_connection(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier, suffix: str
):
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")
    await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        f"finance-{suffix}@example.org",
        "finance",
        needs_mfa=True,
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"media-{suffix}@example.org", "media"
    )
    connection = await create_fake_connection(client, owner_token, org["id"])
    await create_parser_profile(client, owner_token, org["id"], sender_patterns=[SENDER])

    session = await create_session(
        client, media_token, org["id"], mailbox_connection_id=connection["id"]
    )
    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    live = resp.json()
    return org, owner_token, media_token, connection, live


async def test_operator_warning_clear_for_healthy_live_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "healthy"
    )
    # A fresh connection whose watermark was just set counts as "recently
    # active" even with no sync yet -- no false alarm immediately after go-live.
    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json()["operator_warning"] is None


async def test_operator_warning_flags_stale_mailbox_connection(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "stale"
    )

    result = await db_session.execute(
        select(MailboxConnection).where(MailboxConnection.id == connection["id"])
    )
    conn_row = result.scalar_one()
    conn_row.last_sync_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await db_session.commit()

    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    warning = resp.json()["operator_warning"]
    assert warning is not None
    assert "mailbox activity" in warning.lower()


async def test_operator_warning_flags_pending_reconciliation(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "recon"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-ambiguous",
        sender=SENDER,
        subject="Deposit received",
        body="Funds have been deposited to your account.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "ambiguous"

    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    warning = resp.json()["operator_warning"]
    assert warning is not None
    assert "reconciliation" in warning.lower()

    # Never leaks to the public/display payload.
    resp = await client.post(
        f"/v1/sessions/{live['id']}/display-token", headers={"Authorization": f"Bearer {media_token}"}
    )
    display_token = resp.json()["display_token"]
    resp = await client.get(
        f"/v1/sessions/{live['id']}/public", headers={"Authorization": f"Bearer {display_token}"}
    )
    assert "operator_warning" not in resp.json()


async def test_revoked_connection_stops_ingestion_and_warns_operator(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """Acceptance criterion (spec 15): "Mailbox revocation prevents further
    retrieval immediately and surfaces a private health alert."
    """
    org, owner_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "revoke"
    )

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "revoked"

    # Further retrieval stops immediately: a webhook for the now-revoked
    # connection is acknowledged but not processed.
    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-after-revoke",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $10.00 CAD.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"

    resp = await client.get(
        f"/v1/sessions/{live['id']}/events", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json() == []  # nothing was ingested from the revoked connection

    # A private health alert surfaces to the operator...
    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    warning = resp.json()["operator_warning"]
    assert warning is not None
    assert "revoked" in warning.lower()

    # ...but never reaches the public display.
    resp = await client.post(
        f"/v1/sessions/{live['id']}/display-token", headers={"Authorization": f"Bearer {media_token}"}
    )
    display_token = resp.json()["display_token"]
    resp = await client.get(
        f"/v1/sessions/{live['id']}/public", headers={"Authorization": f"Bearer {display_token}"}
    )
    assert "operator_warning" not in resp.json()
