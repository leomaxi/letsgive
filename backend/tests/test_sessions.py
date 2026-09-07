from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.approval import Approval
from tests.conftest import CapturingNotifier
from tests.helpers import (
    add_active_member,
    approve_session,
    create_org,
    create_session,
    enable_mfa,
    register_and_login,
)


async def _org_with_finance_and_media(client: AsyncClient, db_session: AsyncSession, suffix: str):
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
    return org, owner_token, finance_token, media_token


async def test_full_session_lifecycle(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "life"
    )

    session = await create_session(client, media_token, org["id"], duration_seconds=1800)
    assert session["status"] == "draft"
    assert session["currency"] == "CAD"

    authorized = await approve_session(client, notifier, media_token, session["id"])
    assert authorized["status"] == "authorized"
    assert authorized["watermark"] is not None
    assert notifier.sent[0]["to_email"] == "finance-life@example.org"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    live = resp.json()
    assert live["status"] == "live"
    assert live["ends_at"] is not None

    resp = await client.patch(
        f"/v1/sessions/{session['id']}/visibility",
        json={"amount_visible": True, "expected_version": live["version"]},
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["amount_visible"] is True
    visible = resp.json()

    resp = await client.post(
        f"/v1/sessions/{session['id']}/pause",
        json={"expected_version": visible["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    paused = resp.json()
    assert paused["status"] == "paused"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/resume",
        json={"expected_version": paused["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    resumed = resp.json()
    assert resumed["status"] == "live"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/extend",
        json={"additional_seconds": 300, "expected_version": resumed["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    extended = resp.json()
    assert extended["ends_at"] > resumed["ends_at"]

    resp = await client.post(
        f"/v1/sessions/{session['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    display_token = resp.json()["display_token"]

    resp = await client.get(
        f"/v1/sessions/{session['id']}/public",
        headers={"Authorization": f"Bearer {display_token}"},
    )
    assert resp.status_code == 200, resp.text
    public = resp.json()
    assert public["status"] == "live"
    assert public["organization_name"] == org["name"]
    assert public["amount_visible"] is True
    assert public["contribution_count"] == 0
    assert set(public.keys()) == {
        "status",
        "organization_name",
        "currency",
        "ends_at",
        "goal_amount",
        "amount_visible",
        "contribution_count",
        "total_amount",
        "goal_reached",
    }

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": extended["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"


async def test_public_payload_reveals_goal_reached_even_when_amount_is_hidden(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    # Real production bug: a real deposit pushed the total past the goal,
    # but the celebration never fired on either the operator console or the
    # public projection page, because both needlessly re-gated an
    # already-known "did we hit it" fact behind amount_visible -- which is
    # only ever supposed to control whether the *exact running total* is
    # public, not whether the binary goal-reached milestone is.
    org, owner_token, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "goalreached"
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 1800,
            "test_mode": True,
            "goal_enabled": True,
            "goal_amount": "2.00",
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()
    assert session["goal_amount"] == "2.00"

    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    # amount_visible is deliberately left False (the default) -- the
    # audience never sees the exact running total for this session.
    resp = await client.post(
        f"/v1/sessions/{session['id']}/simulate-deposit",
        json={"amount": "1.00"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/display-token", headers={"Authorization": f"Bearer {media_token}"}
    )
    display_token = resp.json()["display_token"]

    resp = await client.get(
        f"/v1/sessions/{session['id']}/public", headers={"Authorization": f"Bearer {display_token}"}
    )
    public = resp.json()
    assert public["amount_visible"] is False
    assert public["total_amount"] is None  # exact figure still hidden
    assert public["goal_reached"] is False  # $1.00 < $2.00 goal

    resp = await client.post(
        f"/v1/sessions/{session['id']}/simulate-deposit",
        json={"amount": "1.02"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(
        f"/v1/sessions/{session['id']}/public", headers={"Authorization": f"Bearer {display_token}"}
    )
    public = resp.json()
    # $2.02 total now clears the $2.00 goal -- reached is True even though
    # the exact total ($2.02) stays hidden from the public payload.
    assert public["amount_visible"] is False
    assert public["total_amount"] is None
    assert public["goal_reached"] is True

    # The operator's own console, unlike the public payload, always carries
    # the real total regardless of amount_visible -- it's a private view.
    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    operator = resp.json()
    assert operator["amount_visible"] is False
    assert operator["total_amount"] == "2.02"


async def test_owner_can_create_and_run_a_session_solo(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """Owner gained the same session-control rights as Media (create,
    request-approval, verify, start, simulate-deposit, display-token, close)
    so a one-person org isn't locked out of the product just for having no
    separate Media user. The one control that stays a genuinely separate
    security boundary is untouched: the OTP itself must still be relayed by
    a real, distinct Finance officer (spec 4) -- this org has no Media user
    at all, only Owner + Finance, and the whole lifecycle still works.
    """
    owner_token = await register_and_login(client, "owner-solo@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Solo Org")
    finance_token = await add_active_member(
        client, db_session, owner_token, org["id"], "finance-solo@example.org", "finance", needs_mfa=True
    )

    session = await create_session(client, owner_token, org["id"], test_mode=True)
    assert session["status"] == "draft"

    authorized = await approve_session(client, notifier, owner_token, session["id"])
    assert authorized["status"] == "authorized"
    # The code was still sent to the real Finance officer, not the Owner --
    # Owner can request/verify, but never receives the code itself.
    assert notifier.sent[0]["to_email"] == "finance-solo@example.org"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    live = resp.json()

    resp = await client.post(
        f"/v1/sessions/{session['id']}/simulate-deposit",
        json={"amount": "25.00"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/display-token",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": live["version"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"
    # The Finance-only visibility toggle stays exactly that -- Owner is not
    # a substitute for Finance everywhere, only for Media.
    resp = await client.patch(
        f"/v1/sessions/{session['id']}/visibility",
        json={"amount_visible": True, "expected_version": resp.json()["version"]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 403


async def test_request_approval_without_finance_officer_fails(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-nofin@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "No Finance Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-nofin@example.org", "media"
    )

    session = await create_session(client, media_token, org["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 400


async def test_wrong_code_locks_after_max_attempts(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "lock")
    session = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    approval_id = resp.json()["approval_id"]

    last_resp = None
    for _ in range(5):
        last_resp = await client.post(
            f"/v1/sessions/{session['id']}/verify",
            json={"approval_id": approval_id, "code": "000000"},
            headers={"Authorization": f"Bearer {media_token}"},
        )
    assert last_resp.status_code == 400
    assert last_resp.json()["detail"] == "code_locked"

    real_code = notifier.latest_code_for(session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/verify",
        json={"approval_id": approval_id, "code": real_code},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_locked"


async def test_expired_code_rejected(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "expired")
    session = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    approval_id = resp.json()["approval_id"]
    code = notifier.latest_code_for(session["id"])

    result = await db_session.execute(select(Approval).where(Approval.id == approval_id))
    approval = result.scalar_one()
    approval.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        f"/v1/sessions/{session['id']}/verify",
        json={"approval_id": approval_id, "code": code},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "code_expired"


async def test_optimistic_lock_conflict_on_stale_version(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "optlock")
    session = await create_session(client, media_token, org["id"])
    authorized = await approve_session(client, notifier, media_token, session["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"] + 99},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 409


async def test_visibility_change_requires_finance_role(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "vis")
    session = await create_session(client, media_token, org["id"])
    authorized = await approve_session(client, notifier, media_token, session["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}/visibility",
        json={"amount_visible": True, "expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_public_endpoint_rejects_normal_user_token(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "pubtok")
    session = await create_session(client, media_token, org["id"])

    resp = await client.get(
        f"/v1/sessions/{session['id']}/public",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 401


async def test_display_token_scoped_to_its_own_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "scoped")
    session_a = await create_session(client, media_token, org["id"])
    session_b = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/sessions/{session_a['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    token_a = resp.json()["display_token"]

    resp = await client.get(
        f"/v1/sessions/{session_b['id']}/public",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 401


async def test_non_member_cannot_view_session_operator_state(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "outsider")
    session = await create_session(client, media_token, org["id"])

    outsider_token = await register_and_login(client, "outsider-sess@example.org")
    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


async def test_owner_and_auditor_can_view_but_only_auditor_cannot_control_session_operator_state(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """Read access to GET .../operator is any active member (Owner/Auditor
    included) -- the sessions list links every role to this page, so a
    Media/Finance-only read would 403 an Owner or Auditor just clicking
    through. Control actions (start/pause/etc.) stay Auditor-excluded, but
    Owner gained the same control rights as Media (a solo-admin org has no
    one else to create/run a session; the actual finance-approval OTP relay
    still requires a distinct, real Finance officer, so that separation of
    duties is untouched).
    """
    org, owner_token, _, media_token = await _org_with_finance_and_media(client, db_session, "viewonly")
    session = await create_session(client, media_token, org["id"])
    auditor_token = await add_active_member(
        client, db_session, owner_token, org["id"], "auditor-viewonly@example.org", "auditor"
    )

    for token in (owner_token, auditor_token):
        resp = await client.get(
            f"/v1/sessions/{session['id']}/operator",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

        resp = await client.get(
            f"/v1/sessions/{session['id']}/events",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {auditor_token}"},
    )
    assert resp.status_code == 403


async def test_list_org_sessions_ordered_newest_first_and_filterable(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "list"
    )
    first = await create_session(client, media_token, org["id"])
    second = await create_session(client, media_token, org["id"])

    resp = await client.get(
        f"/v1/organizations/{org['id']}/sessions",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    sessions = resp.json()
    assert [s["id"] for s in sessions] == [second["id"], first["id"]]

    authorized = await approve_session(client, notifier, media_token, second["id"])
    resp = await client.post(
        f"/v1/sessions/{second['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200

    resp = await client.get(
        f"/v1/organizations/{org['id']}/sessions?status=live",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    live_sessions = resp.json()
    assert [s["id"] for s in live_sessions] == [second["id"]]


async def test_outsider_cannot_list_org_sessions(client: AsyncClient, db_session: AsyncSession):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "listrbac")
    await create_session(client, media_token, org["id"])

    outsider_token = await register_and_login(client, "outsider-list@example.org")
    resp = await client.get(
        f"/v1/organizations/{org['id']}/sessions",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


async def test_can_cancel_a_draft_session(client: AsyncClient, db_session: AsyncSession):
    org, _, _, media_token = await _org_with_finance_and_media(client, db_session, "canceldraft")
    session = await create_session(client, media_token, org["id"])
    assert session["status"] == "draft"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": session["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"


async def test_can_cancel_an_approval_requested_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "cancelrequested"
    )
    session = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json()["status"] == "approval_requested"
    current_version = resp.json()["version"]

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": current_version},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"


async def test_can_cancel_an_authorized_but_not_yet_started_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, _, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "cancelauth"
    )
    session = await create_session(client, media_token, org["id"])
    authorized = await approve_session(client, notifier, media_token, session["id"])
    assert authorized["status"] == "authorized"

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"
