from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_org, enable_mfa, register_and_login


async def test_non_member_cannot_read_another_orgs_data(client: AsyncClient):
    owner_token = await register_and_login(client, "owner-a@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org A")

    outsider_token = await register_and_login(client, "outsider@example.org")

    resp = await client.get(
        f"/v1/organizations/{org['id']}",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404

    resp = await client.get(
        f"/v1/organizations/{org['id']}/members",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404


async def test_non_owner_cannot_invite_members(client: AsyncClient):
    owner_token = await register_and_login(client, "owner-b@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org B")

    auditor_token = await register_and_login(client, "auditor-b@example.org")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "auditor-b@example.org", "role": "auditor"},
        headers=owner_headers,
    )
    assert resp.status_code == 201
    invited_membership_id = resp.json()["id"]
    assert resp.json()["role"] == "auditor"

    resp = await client.post(
        f"/v1/me/invitations/{invited_membership_id}/accept",
        headers={"Authorization": f"Bearer {auditor_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "owner-b@example.org", "role": "media"},
        headers={"Authorization": f"Bearer {auditor_token}"},
    )
    assert resp.status_code == 403


async def test_finance_role_requires_invitee_mfa(client: AsyncClient):
    owner_token = await register_and_login(client, "owner-c@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org C")

    await register_and_login(client, "nomfa-c@example.org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "nomfa-c@example.org", "role": "finance"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400


async def test_list_my_organizations_only_returns_active_memberships(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-d@example.org")
    await enable_mfa(client, owner_token)
    org_d = await create_org(client, owner_token, "Org D")
    await create_org(client, owner_token, "Org E")

    resp = await client.get(
        "/v1/organizations", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert resp.status_code == 200, resp.text
    orgs = resp.json()
    assert {o["name"] for o in orgs} == {"Org D", "Org E"}
    assert all(o["role"] == "owner" for o in orgs)

    # An outsider with no membership at all sees an empty list, not org D/E.
    outsider_token = await register_and_login(client, "outsider-d@example.org")
    resp = await client.get(
        "/v1/organizations", headers={"Authorization": f"Bearer {outsider_token}"}
    )
    assert resp.json() == []

    # An invited-but-not-yet-active member doesn't see the org either.
    await client.post(
        f"/v1/organizations/{org_d['id']}/members/invite",
        json={"email": "outsider-d@example.org", "role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    resp = await client.get(
        "/v1/organizations", headers={"Authorization": f"Bearer {outsider_token}"}
    )
    assert resp.json() == []


async def test_invitation_accept_flow(client: AsyncClient):
    owner_token = await register_and_login(client, "owner-f@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org F")

    invitee_token = await register_and_login(client, "invitee-f@example.org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "invitee-f@example.org", "role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201
    membership_id = resp.json()["id"]

    invitee_headers = {"Authorization": f"Bearer {invitee_token}"}

    resp = await client.get("/v1/me/invitations", headers=invitee_headers)
    assert resp.status_code == 200, resp.text
    assert [i["organization_name"] for i in resp.json()] == ["Org F"]

    # The org doesn't show up as a member org until the invite is accepted.
    resp = await client.get("/v1/organizations", headers=invitee_headers)
    assert resp.json() == []

    resp = await client.post(
        f"/v1/me/invitations/{membership_id}/accept", headers=invitee_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"

    resp = await client.get("/v1/me/invitations", headers=invitee_headers)
    assert resp.json() == []

    resp = await client.get("/v1/organizations", headers=invitee_headers)
    assert {o["name"] for o in resp.json()} == {"Org F"}

    # Accepting again 404s -- it's no longer in the invited state.
    resp = await client.post(
        f"/v1/me/invitations/{membership_id}/accept", headers=invitee_headers
    )
    assert resp.status_code == 404


async def test_invitation_decline_and_only_invitee_can_act_on_it(client: AsyncClient):
    owner_token = await register_and_login(client, "owner-g@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Org G")

    invitee_token = await register_and_login(client, "invitee-g@example.org")
    other_token = await register_and_login(client, "other-g@example.org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "invitee-g@example.org", "role": "auditor"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    membership_id = resp.json()["id"]

    # A different user can't accept or decline someone else's invitation.
    resp = await client.post(
        f"/v1/me/invitations/{membership_id}/accept",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 404

    resp = await client.post(
        f"/v1/me/invitations/{membership_id}/decline",
        headers={"Authorization": f"Bearer {invitee_token}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        "/v1/me/invitations", headers={"Authorization": f"Bearer {invitee_token}"}
    )
    assert resp.json() == []

    # Declining freed the slot up -- the owner can invite the same person again.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": "invitee-g@example.org", "role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201
