from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import (
    add_active_member,
    create_fake_connection,
    create_org,
    enable_mfa,
    register_and_login,
)


async def test_media_can_list_but_not_create_or_revoke_connections(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-conn@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Conn Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-conn@example.org", "media"
    )

    connection = await create_fake_connection(client, owner_token, org["id"])

    # Media needs read access to pick a connection when creating a session.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert "webhook_secret" not in resp.json()[0]
    assert "token_ref" not in resp.json()[0]

    # But only Owner/Finance can create or revoke one.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections",
        json={"provider": "fake", "mailbox": "second@church.org"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_outsider_cannot_list_connections(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-conn2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Conn Org 2")
    await create_fake_connection(client, owner_token, org["id"])

    outsider_token = await register_and_login(client, "outsider-conn@example.org")
    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404
