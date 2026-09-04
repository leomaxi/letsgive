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
    }

    resp = await client.post(
        f"/v1/sessions/{session['id']}/close",
        json={"expected_version": extended["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"


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


async def test_owner_and_auditor_can_view_but_not_control_session_operator_state(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """Read access to GET .../operator is any active member (Owner/Auditor
    included) -- the sessions list links every role to this page, so a
    Media/Finance-only read would 403 an Owner or Auditor just clicking
    through. Control actions (start/pause/etc.) stay Media/Finance-only.
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
            headers={"Authorization": f"Bearer {token}"},
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
