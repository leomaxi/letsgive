from datetime import datetime, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import (
    add_active_member,
    create_fake_connection,
    create_org,
    create_parser_profile,
    deliver_webhook,
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


async def test_owner_can_update_and_delete_a_parser_profile(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-profile@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Profile Org")

    profile = await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@bank.com"]
    )
    assert profile["is_active"] is True

    resp = await client.patch(
        f"/v1/organizations/{org['id']}/parser-profiles/{profile['id']}",
        json={
            "name": "Renamed Bank",
            "sender_patterns": ["notifications@bank.com", "@bank.com"],
            "confidence_threshold": 0.9,
            "is_active": False,
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["name"] == "Renamed Bank"
    assert updated["sender_patterns"] == ["notifications@bank.com", "@bank.com"]
    assert updated["confidence_threshold"] == 0.9
    assert updated["is_active"] is False
    # A partial update -- fields not supplied stay untouched.
    assert updated["default_currency"] == profile["default_currency"]

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/parser-profiles/{profile['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        f"/v1/organizations/{org['id']}/parser-profiles",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.json() == []


async def test_media_cannot_update_or_delete_a_parser_profile(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-profile2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Profile Org 2")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-profile@example.org", "media"
    )
    profile = await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@bank.com"]
    )

    resp = await client.patch(
        f"/v1/organizations/{org['id']}/parser-profiles/{profile['id']}",
        json={"name": "Hijacked"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/parser-profiles/{profile['id']}",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_cannot_update_or_delete_another_orgs_parser_profile(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-profile3@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Profile Org 3")
    profile = await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@bank.com"]
    )

    other_owner_token = await register_and_login(client, "owner-profile4@example.org")
    await enable_mfa(client, other_owner_token)
    other_org = await create_org(client, other_owner_token, "Profile Org 4")

    resp = await client.patch(
        f"/v1/organizations/{other_org['id']}/parser-profiles/{profile['id']}",
        json={"name": "Stolen"},
        headers={"Authorization": f"Bearer {other_owner_token}"},
    )
    assert resp.status_code == 404

    resp = await client.delete(
        f"/v1/organizations/{other_org['id']}/parser-profiles/{profile['id']}",
        headers={"Authorization": f"Bearer {other_owner_token}"},
    )
    assert resp.status_code == 404


async def test_owner_can_delete_a_revoked_connection_with_no_history(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-delconn@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Delete Conn Org")
    connection = await create_fake_connection(client, owner_token, org["id"])

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 204, resp.text

    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.json() == []


async def test_cannot_delete_a_connection_that_is_not_revoked(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-delconn2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Delete Conn Org 2")
    connection = await create_fake_connection(client, owner_token, org["id"])

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "revoked" in resp.json()["detail"].lower()

    # Still there -- the failed delete didn't remove it.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/connections",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert len(resp.json()) == 1


async def test_cannot_delete_a_revoked_connection_with_ledger_history(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-delconn3@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Delete Conn Org 3")
    connection = await create_fake_connection(client, owner_token, org["id"])
    await create_parser_profile(
        client, owner_token, org["id"], sender_patterns=["notifications@fakebank.com"]
    )

    # No active session, so this lands as excluded_no_active_session -- but a
    # ContributionEvent row still gets written either way (see
    # app/domain/ingestion.py), which is all the delete guard cares about.
    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="msg-history",
        sender="notifications@fakebank.com",
        subject="Deposit received",
        body="You have received $5.00 CAD.",
        received_at=datetime.now(timezone.utc),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "processed real deposit events" in resp.json()["detail"]


async def test_media_cannot_delete_a_connection(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-delconn4@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Delete Conn Org 4")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-delconn@example.org", "media"
    )
    connection = await create_fake_connection(client, owner_token, org["id"])
    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403


async def test_cannot_delete_another_orgs_connection(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-delconn5@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Delete Conn Org 5")
    connection = await create_fake_connection(client, owner_token, org["id"])
    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections/{connection['id']}/revoke",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    other_owner_token = await register_and_login(client, "owner-delconn6@example.org")
    await enable_mfa(client, other_owner_token)
    other_org = await create_org(client, other_owner_token, "Delete Conn Org 6")

    resp = await client.delete(
        f"/v1/organizations/{other_org['id']}/connections/{connection['id']}",
        headers={"Authorization": f"Bearer {other_owner_token}"},
    )
    assert resp.status_code == 404
