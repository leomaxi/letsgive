from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import add_active_member, create_org, enable_mfa, register_and_login


async def test_owner_can_change_team_member_role(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "role-owner@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Role Org")
    member_token = await add_active_member(
        client, db_session, owner_token, org["id"], "role-member@example.org", "media"
    )

    resp = await client.get(
        f"/v1/organizations/{org['id']}/members",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    member = next(m for m in resp.json() if m["user_email"] == "role-member@example.org")

    resp = await client.patch(
        f"/v1/organizations/{org['id']}/members/{member['id']}",
        json={"role": "auditor"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "auditor"

    resp = await client.get(
        f"/v1/organizations/{org['id']}/members",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert resp.status_code == 200, resp.text
    changed = next(m for m in resp.json() if m["user_email"] == "role-member@example.org")
    assert changed["role"] == "auditor"


async def test_role_change_enforces_mfa_and_last_owner(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "role-owner-guard@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Role Guard Org")
    await add_active_member(
        client, db_session, owner_token, org["id"], "role-no-mfa@example.org", "media"
    )

    resp = await client.get(
        f"/v1/organizations/{org['id']}/members",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    members = resp.json()
    owner = next(m for m in members if m["user_email"] == "role-owner-guard@example.org")
    media = next(m for m in members if m["user_email"] == "role-no-mfa@example.org")

    resp = await client.patch(
        f"/v1/organizations/{org['id']}/members/{media['id']}",
        json={"role": "finance"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "MFA" in resp.json()["detail"]

    resp = await client.patch(
        f"/v1/organizations/{org['id']}/members/{owner['id']}",
        json={"role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "owner" in resp.json()["detail"].lower()

