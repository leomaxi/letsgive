from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

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


async def _live_session_with_connection(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier, suffix: str
):
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")

    finance_token = await add_active_member(
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
    assert connection["status"] == "connected"

    await create_parser_profile(client, owner_token, org["id"], sender_patterns=[SENDER])

    session = await create_session(
        client, media_token, org["id"], mailbox_connection_id=connection["id"]
    )
    assert session["mailbox_connection_id"] == connection["id"]

    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    live = resp.json()

    return org, owner_token, finance_token, media_token, connection, live


async def test_accepted_deposit_counts_toward_totals(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "acc"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-1",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $42.50 CAD. Funds deposited to your account.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.status_code == 200, resp.text
    ack = resp.json()
    assert ack["status"] == "processed"
    assert ack["decision"] == "accepted"
    assert ack["already_processed"] is False

    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    operator = resp.json()
    assert operator["contribution_count"] == 1
    assert operator["total_amount"] == "42.50"

    resp = await client.patch(
        f"/v1/sessions/{live['id']}/visibility",
        json={"amount_visible": True, "expected_version": operator["version"]},
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{live['id']}/display-token", headers={"Authorization": f"Bearer {media_token}"}
    )
    display_token = resp.json()["display_token"]
    resp = await client.get(
        f"/v1/sessions/{live['id']}/public", headers={"Authorization": f"Bearer {display_token}"}
    )
    public = resp.json()
    assert public["contribution_count"] == 1
    assert public["total_amount"] == "42.50"


async def test_duplicate_webhook_delivery_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "dup"
    )

    kwargs = dict(
        client=client,
        db_session=db_session,
        connection_id=connection["id"],
        provider_message_id="msg-dup",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $10.00 CAD.",
        received_at=datetime.now(timezone.utc),
    )
    first = await deliver_webhook(**kwargs)
    second = await deliver_webhook(**kwargs)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["already_processed"] is False
    assert second.json()["already_processed"] is True
    assert first.json()["event_id"] == second.json()["event_id"]

    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json()["contribution_count"] == 1


async def test_message_before_watermark_excluded_as_time_window(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "early"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-early",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $5.00 CAD.",
        received_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert resp.json()["decision"] == "excluded_time_window"

    # Attributed to the session for reconciliation review even though it
    # doesn't count toward its total (spec 7: borderline cases get queued).
    resp = await client.get(
        f"/v1/sessions/{live['id']}/events", headers={"Authorization": f"Bearer {media_token}"}
    )
    events = resp.json()
    assert len(events) == 1
    assert events[0]["decision"] == "excluded_time_window"

    resp = await client.get(
        f"/v1/organizations/{org['id']}/reconciliation",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    queue = resp.json()
    assert len(queue) == 1
    assert queue[0]["contribution_event_id"] == events[0]["id"]


async def test_sender_mismatch_excluded(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "src"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-src",
        sender="not-the-bank@example.com",
        subject="Deposit received",
        body="You have received $5.00 CAD.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "excluded_source_mismatch"


async def test_no_credit_intent_excluded(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "reject"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-reject",
        sender=SENDER,
        subject="Transfer request reminder",
        body="This is a reminder that a transfer was requested and later cancelled.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "excluded_no_credit_intent"


async def test_low_confidence_is_ambiguous_and_not_counted(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "ambig"
    )

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-ambig",
        sender=SENDER,
        subject="Deposit received",
        body="Funds have been deposited to your account.",  # credit intent, no amount
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "ambiguous"

    resp = await client.get(
        f"/v1/sessions/{live['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json()["contribution_count"] == 0


async def test_more_specific_parser_profile_wins_over_a_broader_overlapping_one(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """An org with genuinely overlapping profiles gets deterministic,
    sensible resolution: the more specific match wins (spec 5.3's parser
    profile matching), not whichever profile the database happens to list
    first or most recently. The broad profile is deliberately created
    *first* and the exact one *second* -- if selection still fell back to
    creation order (in either direction), this would prove nothing; it has
    to be the specificity rule doing the work.
    """
    owner_token = await register_and_login(client, "owner-specificity@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org Specificity")
    await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        "finance-specificity@example.org",
        "finance",
        needs_mfa=True,
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-specificity@example.org", "media"
    )
    connection = await create_fake_connection(client, owner_token, org["id"])

    await create_parser_profile(
        client,
        owner_token,
        org["id"],
        sender_patterns=["@fakebank.com"],
        name="Broad catch-all",
        template_version="v1-broad",
    )
    await create_parser_profile(
        client,
        owner_token,
        org["id"],
        sender_patterns=[SENDER],
        name="Exact address",
        template_version="v2-exact",
    )

    session = await create_session(
        client, media_token, org["id"], mailbox_connection_id=connection["id"]
    )
    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    live = resp.json()

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-specificity",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $15.00 CAD. Funds deposited to your account.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "accepted"

    resp = await client.get(
        f"/v1/sessions/{live['id']}/events", headers={"Authorization": f"Bearer {media_token}"}
    )
    events = resp.json()
    event = next(e for e in events if e["provider_message_id"] == "msg-specificity")
    assert event["template_version"] == "v2-exact"


async def test_no_active_session_excluded(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    owner_token = await register_and_login(client, "owner-noactive@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "No Active Org")
    connection = await create_fake_connection(client, owner_token, org["id"])
    await create_parser_profile(client, owner_token, org["id"], sender_patterns=[SENDER])

    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-noactive",
        sender=SENDER,
        subject="Deposit received",
        body="You have received $5.00 CAD.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "excluded_no_active_session"


async def test_invalid_webhook_signature_rejected(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "badsig"
    )

    resp = await client.post(
        "/v1/providers/fake/webhook",
        content=b'{"connection_id": "%s", "provider_message_id": "x"}' % connection["id"].encode(),
        headers={"Content-Type": "application/json", "X-LetsGive-Signature": "0" * 64},
    )
    assert resp.status_code == 401


async def test_simulate_deposit_requires_test_mode(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token, connection, live = await _live_session_with_connection(
        client, db_session, notifier, "simreal"
    )

    resp = await client.post(
        f"/v1/sessions/{live['id']}/simulate-deposit",
        json={"amount": "20.00"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 400


async def test_simulate_deposit_in_test_mode_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    owner_token = await register_and_login(client, "owner-sim@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Sim Org")
    finance_token = await add_active_member(
        client, db_session, owner_token, org["id"], "finance-sim@example.org", "finance", needs_mfa=True
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-sim@example.org", "media"
    )

    session = await create_session(client, media_token, org["id"], test_mode=True)
    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/simulate-deposit",
        json={"amount": "20.00"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 201, resp.text
    event = resp.json()
    assert event["test_mode"] is True
    assert event["decision"] == "accepted"

    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert resp.json()["contribution_count"] == 1
    assert resp.json()["total_amount"] == "20.00"
