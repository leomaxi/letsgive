from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import add_active_member, create_org, enable_mfa, register_and_login


async def test_org_creation_returns_a_join_code(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-jc1@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 1")
    assert isinstance(org["join_code"], str)
    assert len(org["join_code"]) == 8


async def test_full_join_request_lifecycle_approve(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-jc2@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 2")

    requester_token = await register_and_login(client, "requester-jc2@example.org")

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 201, resp.text
    join_request = resp.json()
    assert join_request["organization_name"] == "Join Org 2"
    assert join_request["status"] == "requested"

    # Requester sees it under their own pending requests...
    resp = await client.get(
        "/v1/me/join-requests", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1

    # ...but has no actual access to the org yet (REQUESTED grants nothing).
    resp = await client.get(
        f"/v1/organizations/{org['id']}", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.status_code == 404

    # A REQUESTED row must not show up in the regular members list either
    # (only the Owner, from org creation, is an actual member so far).
    resp = await client.get(
        f"/v1/organizations/{org['id']}/members", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert [m["role"] for m in resp.json()] == ["owner"]

    # Owner sees and approves it, assigning a role.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/join-requests",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.json()[0]["user_email"] == "requester-jc2@example.org"

    resp = await client.post(
        f"/v1/organizations/{org['id']}/join-requests/{join_request['id']}/approve",
        json={"role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "media"
    assert resp.json()["status"] == "active"

    # Now they're a real, active member.
    resp = await client.get(
        f"/v1/organizations/{org['id']}", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/v1/organizations/{org['id']}/members", headers={"Authorization": f"Bearer {owner_token}"}
    )
    assert sorted(m["role"] for m in resp.json()) == ["media", "owner"]

    # The pending-request list is empty now.
    resp = await client.get(
        "/v1/me/join-requests", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.json() == []


async def test_owner_can_deny_a_join_request(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-jc3@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 3")
    requester_token = await register_and_login(client, "requester-jc3@example.org")

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    join_request_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/organizations/{org['id']}/join-requests/{join_request_id}/deny",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        "/v1/me/join-requests", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.json() == []

    # Denied, not just hidden -- the same user can request again.
    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_requester_can_cancel_their_own_join_request(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-jc4@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 4")
    requester_token = await register_and_login(client, "requester-jc4@example.org")

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    join_request_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/me/join-requests/{join_request_id}/cancel",
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        "/v1/me/join-requests", headers={"Authorization": f"Bearer {requester_token}"}
    )
    assert resp.json() == []


async def test_invalid_join_code_returns_404(client: AsyncClient, db_session: AsyncSession):
    requester_token = await register_and_login(client, "requester-jc5@example.org")
    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": "NOTREAL1"},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 404


async def test_duplicate_join_request_is_rejected(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-jc6@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 6")
    requester_token = await register_and_login(client, "requester-jc6@example.org")

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 409


async def test_join_code_is_case_and_whitespace_insensitive(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-jc7@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 7")
    requester_token = await register_and_login(client, "requester-jc7@example.org")

    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": f"  {org['join_code'].lower()}  "},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    assert resp.status_code == 201, resp.text


async def test_non_owner_cannot_list_or_approve_or_deny_join_requests(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-jc8@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 8")
    finance_token = await add_active_member(
        client, db_session, owner_token, org["id"], "finance-jc8@example.org", "finance", needs_mfa=True
    )
    requester_token = await register_and_login(client, "requester-jc8@example.org")
    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    join_request_id = resp.json()["id"]

    resp = await client.get(
        f"/v1/organizations/{org['id']}/join-requests",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/organizations/{org['id']}/join-requests/{join_request_id}/approve",
        json={"role": "media"},
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/organizations/{org['id']}/join-requests/{join_request_id}/deny",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 403


async def test_approving_a_finance_role_requires_the_requester_to_already_have_mfa(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-jc9@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Join Org 9")
    requester_token = await register_and_login(client, "requester-jc9@example.org")
    resp = await client.post(
        "/v1/organizations/join",
        json={"join_code": org["join_code"]},
        headers={"Authorization": f"Bearer {requester_token}"},
    )
    join_request_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/organizations/{org['id']}/join-requests/{join_request_id}/approve",
        json={"role": "finance"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 400
    assert "MFA" in resp.json()["detail"]
