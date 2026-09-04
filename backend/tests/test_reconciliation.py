from datetime import datetime, timezone
from decimal import Decimal

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

    return {
        "org": org,
        "owner_token": owner_token,
        "finance_token": finance_token,
        "media_token": media_token,
        "connection": connection,
        "session": live,
    }


async def _deliver_ambiguous(client, db_session, ctx, provider_message_id: str):
    resp = await deliver_webhook(
        client,
        db_session,
        ctx["connection"]["id"],
        provider_message_id=provider_message_id,
        sender=SENDER,
        subject="Deposit received",
        body="Funds have been deposited to your account.",  # credit intent, no amount
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "ambiguous"
    return resp.json()


async def _deliver_accepted(client, db_session, ctx, provider_message_id: str, amount: str = "42.50"):
    resp = await deliver_webhook(
        client,
        db_session,
        ctx["connection"]["id"],
        provider_message_id=provider_message_id,
        sender=SENDER,
        subject="Deposit received",
        body=f"You have received ${amount} CAD. Funds deposited to your account.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.json()["decision"] == "accepted"
    return resp.json()


async def test_ambiguous_event_appears_in_queue_and_can_be_accepted(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "acc")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")

    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    queue = resp.json()
    assert len(queue) == 1
    item = queue[0]
    assert item["status"] == "pending"
    assert item["reason"]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "accepted", "corrected_amount": "40.00", "note": "Confirmed with finance"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    resolved = resp.json()
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "accepted"
    assert resolved["corrected_amount"] == "40.00"

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/operator",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    operator = resp.json()
    assert operator["contribution_count"] == 1
    assert operator["total_amount"] == "40.00"

    # Original event is untouched -- still ambiguous, not silently overwritten.
    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/events",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    events = resp.json()
    decisions = {e["decision"] for e in events}
    assert decisions == {"ambiguous", "accepted"}
    accepted_event = next(e for e in events if e["decision"] == "accepted")
    ambiguous_event = next(e for e in events if e["decision"] == "ambiguous")
    assert accepted_event["corrects_event_id"] == ambiguous_event["id"]
    assert ambiguous_event["amount"] is None


async def test_resolve_as_excluded_creates_no_new_event(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "exc")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")

    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "excluded", "note": "Not a real deposit"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"] == "excluded"

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/operator",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.json()["contribution_count"] == 0

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/events",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    assert len(resp.json()) == 1  # no correction event added


async def test_cannot_resolve_twice(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "twice")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "excluded"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "excluded"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 409


async def test_media_cannot_resolve(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "rbac")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "excluded"},
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    assert resp.status_code == 403


async def test_session_report_and_csv_export(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "report")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["session_id"] == ctx["session"]["id"]
    assert report["excluded_counts"].get("ambiguous") == 1
    assert len(report["approval_history"]) == 1
    assert report["approval_history"][0]["verified_at"] is not None
    assert report["connection_health"]["connection_id"] == ctx["connection"]["id"]

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}/export.csv",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    body = resp.text
    assert "event_id,received_at,decision" in body
    assert "ambiguous" in body


async def test_media_cannot_view_report(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "reportrbac")
    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    assert resp.status_code == 403


async def test_reversal_nets_the_original_contribution_back_out(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """spec 7's 'reversed' review category: a later notification un-does an
    earlier ACCEPTED deposit. Confirms the whole chain -- the reversal
    correction appears as its own immutable ledger row (never mutating the
    original), the session's live count/total net back to zero, and the
    report reflects the same net totals.
    """
    ctx = await _live_session_with_connection(client, db_session, notifier, "rev")
    accepted = await _deliver_accepted(client, db_session, ctx, "msg-accepted")

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/operator",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.json()["contribution_count"] == 1
    assert resp.json()["total_amount"] == "42.50"

    await _deliver_ambiguous(client, db_session, ctx, "msg-reversal-notice")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation?status_filter=pending",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={
            "resolution": "reversed",
            "reverses_event_id": accepted["event_id"],
            "note": "Bank confirmed the e-transfer was returned",
        },
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"] == "reversed"

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/operator",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    operator = resp.json()
    assert operator["contribution_count"] == 0
    assert Decimal(operator["total_amount"]) == Decimal("0.00")

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/events",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    events = resp.json()
    reversal_event = next(e for e in events if e["decision"] == "reversed")
    assert reversal_event["corrects_event_id"] == accepted["event_id"]
    assert reversal_event["amount"] == "42.50"
    # The original accepted event is untouched, not deleted or mutated.
    original_event = next(e for e in events if e["id"] == accepted["event_id"])
    assert original_event["decision"] == "accepted"
    assert original_event["amount"] == "42.50"

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    report = resp.json()
    assert report["validated_count"] == 0
    assert Decimal(report["validated_amount"]) == Decimal("0.00")
    assert report["excluded_counts"].get("reversed") == 1


async def test_reversal_requires_a_valid_accepted_target(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "revvalid")
    ambiguous_evt = await _deliver_ambiguous(client, db_session, ctx, "msg-1")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    # No reverses_event_id at all.
    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "reversed"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 400

    # Points at an event that isn't ACCEPTED (the ambiguous one itself).
    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "reversed", "reverses_event_id": ambiguous_evt["event_id"]},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 400

    # Points at something that doesn't exist.
    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "reversed", "reverses_event_id": "not-a-real-id"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 400


async def test_cannot_reverse_the_same_contribution_twice(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "revtwice")
    accepted = await _deliver_accepted(client, db_session, ctx, "msg-accepted")

    await _deliver_ambiguous(client, db_session, ctx, "msg-reversal-1")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation?status_filter=pending",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item_one = resp.json()[0]
    resp = await client.post(
        f"/v1/reconciliation/{item_one['id']}/resolve",
        json={"resolution": "reversed", "reverses_event_id": accepted["event_id"]},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text

    await _deliver_ambiguous(client, db_session, ctx, "msg-reversal-2")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation?status_filter=pending",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item_two = resp.json()[0]
    resp = await client.post(
        f"/v1/reconciliation/{item_two['id']}/resolve",
        json={"resolution": "reversed", "reverses_event_id": accepted["event_id"]},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 409, resp.text


async def test_duplicate_resolution_behaves_like_excluded_but_is_tracked_distinctly(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _live_session_with_connection(client, db_session, notifier, "dup")
    await _deliver_ambiguous(client, db_session, ctx, "msg-1")
    resp = await client.get(
        f"/v1/organizations/{ctx['org']['id']}/reconciliation",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    item = resp.json()[0]

    resp = await client.post(
        f"/v1/reconciliation/{item['id']}/resolve",
        json={"resolution": "duplicate", "note": "Same e-transfer notified twice"},
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution"] == "duplicate"

    # Same ledger effect as "excluded": no new event, nothing counted.
    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/operator",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    assert resp.json()["contribution_count"] == 0

    resp = await client.get(
        f"/v1/sessions/{ctx['session']['id']}/events",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    assert len(resp.json()) == 1  # no correction event added

    # But the reason survives distinctly in the report's corrections list,
    # instead of collapsing into a generic "excluded".
    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}",
        headers={"Authorization": f"Bearer {ctx['finance_token']}"},
    )
    corrections = resp.json()["corrections"]
    assert len(corrections) == 1
    assert corrections[0]["resolution"] == "duplicate"
