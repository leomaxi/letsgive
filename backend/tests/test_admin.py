from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_org, enable_mfa, make_platform_admin, register_and_login


async def _make_admin(client: AsyncClient, db_session: AsyncSession, email: str) -> str:
    token = await register_and_login(client, email)
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    await make_platform_admin(db_session, resp.json()["id"])
    return token


async def test_non_admin_gets_403_on_admin_routes(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-noadmin@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "No Admin Org")
    headers = {"Authorization": f"Bearer {owner_token}"}

    resp = await client.get("/v1/admin/organizations", headers=headers)
    assert resp.status_code == 403

    resp = await client.get(f"/v1/admin/organizations/{org['id']}", headers=headers)
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/subscription",
        json={"subscription_status": "active"},
        headers=headers,
    )
    assert resp.status_code == 403

    resp = await client.get("/v1/admin/tickets", headers=headers)
    assert resp.status_code == 403

    resp = await client.get("/v1/admin/audit-logs", headers=headers)
    assert resp.status_code == 403


async def test_admin_can_list_and_view_organizations(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-adminlist@example.org")
    await enable_mfa(client, owner_token)
    org1 = await create_org(client, owner_token, "Admin List Org 1")
    org2 = await create_org(client, owner_token, "Admin List Org 2")

    admin_token = await _make_admin(client, db_session, "admin-list@example.org")
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.get("/v1/admin/organizations", headers=headers)
    assert resp.status_code == 200, resp.text
    by_id = {o["id"]: o for o in resp.json()}
    assert org1["id"] in by_id
    assert org2["id"] in by_id
    # The owner is an active member of both orgs they just created.
    assert by_id[org1["id"]]["member_count"] == 1
    assert by_id[org1["id"]]["plan_key"] == "starter"

    resp = await client.get(f"/v1/admin/organizations/{org1['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["member_count"] == 1
    assert detail["connections_total"] == 0

    resp = await client.get("/v1/admin/organizations/not-a-real-org", headers=headers)
    assert resp.status_code == 404


async def test_admin_list_pagination(client: AsyncClient, db_session: AsyncSession):
    owner_token = await register_and_login(client, "owner-adminpage@example.org")
    await enable_mfa(client, owner_token)
    for i in range(3):
        await create_org(client, owner_token, f"Page Org {i}")

    admin_token = await _make_admin(client, db_session, "admin-page@example.org")
    headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.get("/v1/admin/organizations?limit=2&offset=0", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2

    resp = await client.get("/v1/admin/organizations?limit=2&offset=2", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) >= 1


async def test_admin_subscription_change_bypasses_canceled_block(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-admincancel@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Admin Cancel Org")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    await client.post(f"/v1/organizations/{org['id']}/subscription/cancel", headers=owner_headers)

    resp = await client.get("/v1/plans", headers=owner_headers)
    growth_id = next(p["id"] for p in resp.json() if p["key"] == "growth")

    # The tenant's own self-service path stays blocked once canceled.
    resp = await client.post(
        f"/v1/organizations/{org['id']}/subscription/switch-plan",
        json={"plan_id": growth_id},
        headers=owner_headers,
    )
    assert resp.status_code == 400

    admin_token = await _make_admin(client, db_session, "admin-cancel@example.org")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/subscription",
        json={"plan_id": growth_id, "subscription_status": "active"},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["plan_key"] == "growth"
    assert detail["subscription_status"] == "active"

    # Now that admin reactivated it, the tenant's own audit log shows the
    # admin's action with zero code change to that existing endpoint.
    resp = await client.get(f"/v1/organizations/{org['id']}/audit-logs", headers=owner_headers)
    assert resp.status_code == 200, resp.text
    actions = [entry["action"] for entry in resp.json()]
    assert "platform_admin.subscription_changed" in actions


async def test_admin_subscription_update_requires_a_field(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-adminempty@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Admin Empty Org")

    admin_token = await _make_admin(client, db_session, "admin-empty@example.org")
    resp = await client.post(
        f"/v1/admin/organizations/{org['id']}/subscription",
        json={},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 422
