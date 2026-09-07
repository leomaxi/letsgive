from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import add_active_member, create_org, enable_mfa, make_platform_admin, register_and_login


async def _make_admin(client: AsyncClient, db_session: AsyncSession, email: str) -> str:
    token = await register_and_login(client, email)
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    await make_platform_admin(db_session, resp.json()["id"])
    return token


async def test_owner_can_create_and_reply_to_ticket(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-ticket@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org")
    headers = {"Authorization": f"Bearer {owner_token}"}

    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "Deposits not counting", "body": "A real deposit isn't showing up."},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    ticket = resp.json()
    assert ticket["status"] == "open"
    assert len(ticket["messages"]) == 1
    assert ticket["messages"][0]["author_is_admin"] is False

    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets/{ticket['id']}/messages",
        json={"body": "Following up -- still broken."},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get(
        f"/v1/organizations/{org['id']}/support-tickets/{ticket['id']}", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["messages"]) == 2

    resp = await client.get(f"/v1/organizations/{org['id']}/support-tickets", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1


async def test_non_owner_active_member_can_create_and_reply(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-ticket2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org 2")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-ticket@example.org", "media"
    )
    headers = {"Authorization": f"Bearer {media_token}"}

    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "Question", "body": "How do I set this up?"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


async def test_other_org_owner_cannot_see_or_reply_to_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-ticket3@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org 3")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "Private", "body": "Sensitive stuff."},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    ticket_id = resp.json()["id"]

    other_owner_token = await register_and_login(client, "owner-ticket4@example.org")
    await enable_mfa(client, other_owner_token)
    other_org = await create_org(client, other_owner_token, "Ticket Org 4")
    other_headers = {"Authorization": f"Bearer {other_owner_token}"}

    resp = await client.get(
        f"/v1/organizations/{other_org['id']}/support-tickets/{ticket_id}", headers=other_headers
    )
    assert resp.status_code == 404

    resp = await client.post(
        f"/v1/organizations/{other_org['id']}/support-tickets/{ticket_id}/messages",
        json={"body": "trying to reply"},
        headers=other_headers,
    )
    assert resp.status_code == 404


async def test_admin_can_reply_and_notification_and_unread_flag(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-ticket5@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org 5")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "Help", "body": "Something's wrong."},
        headers=owner_headers,
    )
    ticket_id = resp.json()["id"]

    admin_token = await _make_admin(client, db_session, "admin-ticket5@example.org")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Tenant just posted -- admin side should see it as unread.
    resp = await client.get("/v1/admin/tickets", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    listed = next(t for t in resp.json() if t["id"] == ticket_id)
    assert listed["admin_unread"] is True
    assert listed["organization_name"] == "Ticket Org 5"

    # Viewing the ticket clears admin_unread even before replying.
    resp = await client.get(f"/v1/admin/tickets/{ticket_id}", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["admin_unread"] is False

    resp = await client.post(
        f"/v1/admin/tickets/{ticket_id}/messages",
        json={"body": "We're looking into it."},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["author_is_admin"] is True

    # Tenant reply flips admin_unread back to True.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets/{ticket_id}/messages",
        json={"body": "Thanks, any update?"},
        headers=owner_headers,
    )
    assert resp.status_code == 201, resp.text
    # Checked via the list endpoint, not the detail GET -- the detail GET
    # itself clears admin_unread as the "admin viewed it" side effect, so it
    # would always read back False regardless of what we're testing here.
    resp = await client.get("/v1/admin/tickets", headers=admin_headers)
    listed = next(t for t in resp.json() if t["id"] == ticket_id)
    assert listed["admin_unread"] is True

    # The owner (ticket creator) got a notification for the admin's reply.
    resp = await client.get("/v1/me/notifications", headers=owner_headers)
    assert resp.status_code == 200, resp.text
    types = [n["type"] for n in resp.json()]
    assert "support_reply" in types


async def test_admin_can_reply_to_any_orgs_ticket(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-ticket6@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org 6")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "X", "body": "Y"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    ticket_id = resp.json()["id"]

    admin_token = await _make_admin(client, db_session, "admin-ticket6@example.org")
    resp = await client.post(
        f"/v1/admin/tickets/{ticket_id}/messages",
        json={"body": "Admin reply"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_ticket_status_transitions(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-ticket7@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Ticket Org 7")
    resp = await client.post(
        f"/v1/organizations/{org['id']}/support-tickets",
        json={"subject": "X", "body": "Y"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    ticket_id = resp.json()["id"]

    admin_token = await _make_admin(client, db_session, "admin-ticket7@example.org")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(
        f"/v1/admin/tickets/{ticket_id}/status",
        json={"status": "resolved"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "resolved"

    resp = await client.post(
        f"/v1/admin/tickets/{ticket_id}/status",
        json={"status": "closed"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "closed"
