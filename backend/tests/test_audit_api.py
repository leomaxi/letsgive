from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import add_active_member, create_org, enable_mfa, register_and_login


async def test_owner_can_list_audit_logs(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-audit@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Audit Org")

    resp = await client.get(
        f"/v1/organizations/{org['id']}/audit-logs",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    actions = [entry["action"] for entry in resp.json()]
    assert "organization.created" in actions


async def test_media_cannot_list_audit_logs(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-audit2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Audit Org 2")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-audit2@example.org", "media"
    )

    resp = await client.get(
        f"/v1/organizations/{org['id']}/audit-logs",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_auditor_can_list_audit_logs(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-audit3@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Audit Org 3")
    auditor_token = await add_active_member(
        client, db_session, owner_token, org["id"], "auditor-audit3@example.org", "auditor"
    )

    resp = await client.get(
        f"/v1/organizations/{org['id']}/audit-logs",
        headers={"Authorization": f"Bearer {auditor_token}"},
    )
    assert resp.status_code == 200, resp.text
