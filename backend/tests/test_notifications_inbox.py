import re

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.notifications import get_notifier
from app.main import app
from tests.conftest import CapturingNotifier
from tests.helpers import add_active_member, create_org, create_session, enable_mfa, register_and_login


async def _org_with_finance_and_media(client: AsyncClient, db_session: AsyncSession, suffix: str):
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")
    finance_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"finance-{suffix}@example.org", "finance", needs_mfa=True
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"media-{suffix}@example.org", "media"
    )
    return org, owner_token, finance_token, media_token


async def test_request_approval_creates_an_in_app_notification_with_the_code(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    """Finance officers must see their approval code inside the app, not
    just in an email (see app/domain/inbox.py / request_approval).
    """
    org, owner_token, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "notif"
    )
    session = await create_session(client, media_token, org["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    email_code = notifier.latest_code_for(session["id"])

    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert resp.status_code == 200, resp.text
    notifications = resp.json()
    assert len(notifications) == 1
    notification = notifications[0]
    assert notification["type"] == "approval_code"
    assert notification["organization_name"] == org["name"]
    assert notification["session_id"] == session["id"]
    assert email_code in notification["body"]
    assert notification["read_at"] is None

    # Media (the requester) and Owner did not get one -- only the actual
    # finance officer who's meant to relay the code.
    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json() == []
    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert resp.json() == []


async def test_smtp_failure_still_creates_in_app_approval_code(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    class FailingNotifier:
        async def send_otp(self, *, to_email: str, code: str, session_id: str) -> None:
            raise RuntimeError("smtp unavailable")

    app.dependency_overrides[get_notifier] = lambda: FailingNotifier()

    org, _, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "smtpdown"
    )
    session = await create_session(client, media_token, org["id"])

    approval_resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert approval_resp.status_code == 200, approval_resp.text
    approval_id = approval_resp.json()["approval_id"]
    assert approval_resp.json()["sent_to"] == ["finance-smtpdown@example.org"]
    assert approval_resp.json()["delivery_failed_to"] == ["finance-smtpdown@example.org"]

    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert resp.status_code == 200, resp.text
    notification = resp.json()[0]
    match = re.search(r"Code: (\d{6})", notification["body"])
    assert match is not None

    resp = await client.post(
        f"/v1/sessions/{session['id']}/verify",
        json={"approval_id": approval_id, "code": match.group(1)},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "authorized"


async def test_mark_notification_read_and_read_all(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "notifread"
    )
    session_a = await create_session(client, media_token, org["id"])
    session_b = await create_session(client, media_token, org["id"])

    for session in (session_a, session_b):
        resp = await client.post(
            f"/v1/sessions/{session['id']}/request-approval",
            headers={"Authorization": f"Bearer {media_token}"},
        )
        assert resp.status_code == 200, resp.text

    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {finance_token}"}
    )
    notifications = resp.json()
    assert len(notifications) == 2

    resp = await client.post(
        f"/v1/me/notifications/{notifications[0]['id']}/read",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["read_at"] is not None

    resp = await client.get(
        "/v1/me/notifications?unread_only=true", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert len(resp.json()) == 1

    resp = await client.post(
        "/v1/me/notifications/read-all", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert resp.status_code == 204

    resp = await client.get(
        "/v1/me/notifications?unread_only=true", headers={"Authorization": f"Bearer {finance_token}"}
    )
    assert resp.json() == []


async def test_cannot_read_another_users_notification(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    org, owner_token, finance_token, media_token = await _org_with_finance_and_media(
        client, db_session, "notifiso"
    )
    session = await create_session(client, media_token, org["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        "/v1/me/notifications", headers={"Authorization": f"Bearer {finance_token}"}
    )
    notification_id = resp.json()[0]["id"]

    resp = await client.post(
        f"/v1/me/notifications/{notification_id}/read",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 404
