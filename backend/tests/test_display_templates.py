from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import add_active_member, create_org, create_session, enable_mfa, register_and_login


async def _org_with_owner_and_media(client: AsyncClient, db_session: AsyncSession, suffix: str):
    owner_token = await register_and_login(client, f"owner-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"Org {suffix}")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"media-{suffix}@example.org", "media"
    )
    finance_token = await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        f"finance-{suffix}@example.org",
        "finance",
        needs_mfa=True,
    )
    return org, owner_token, media_token, finance_token


async def test_create_template_has_default_canvas(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, _, _ = await _org_with_owner_and_media(client, db_session, "create")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text
    template = resp.json()
    assert template["version"] == 1
    assert template["canvas"]["width"] == 1920
    assert template["canvas"]["height"] == 1080
    assert template["elements"] == []


async def test_save_template_with_elements(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, media_token, _ = await _org_with_owner_and_media(client, db_session, "save")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    template = resp.json()

    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={
            "name": "Sunday Service",
            "canvas": {"width": 1920, "height": 1080, "background_color": "#111111"},
            "is_default": True,
            "expected_version": template["version"],
            "elements": [
                {
                    "type": "contribution_count",
                    "x": 100,
                    "y": 200,
                    "width": 800,
                    "height": 400,
                    "style": {"font_size": 220, "color": "#ffffff"},
                },
                {"type": "heading", "binding": {"text": "Giving in Progress"}},
            ],
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert saved["name"] == "Sunday Service"
    assert saved["is_default"] is True
    assert saved["version"] == 2
    assert len(saved["elements"]) == 2
    types = {el["type"] for el in saved["elements"]}
    assert types == {"contribution_count", "heading"}


async def test_stale_version_conflict(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, _, _ = await _org_with_owner_and_media(client, db_session, "stale")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()

    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={"name": "V2", "expected_version": template["version"] + 5, "elements": []},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 409


async def test_only_one_default_template_at_a_time(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, _, _ = await _org_with_owner_and_media(client, db_session, "onedefault")

    async def make_default(name: str) -> dict:
        resp = await client.post(
            f"/v1/organizations/{org['id']}/display-templates",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        t = resp.json()
        resp = await client.put(
            f"/v1/organizations/{org['id']}/display-templates/{t['id']}",
            json={"name": name, "is_default": True, "expected_version": t["version"], "elements": []},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    first = await make_default("First")
    second = await make_default("Second")
    assert second["is_default"] is True

    resp = await client.get(
        f"/v1/organizations/{org['id']}/display-templates/{first['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.json()["is_default"] is False


async def test_new_session_auto_binds_default_template(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, media_token, _ = await _org_with_owner_and_media(client, db_session, "autobind")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()
    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={
            "name": "Default",
            "is_default": True,
            "expected_version": template["version"],
            "elements": [],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 200, resp.text

    session = await create_session(client, media_token, org["id"])
    assert session["display_template_id"] == template["id"]


async def test_duplicate_template_copies_elements_and_resets_default(
    client: AsyncClient, db_session: AsyncSession
):
    org, owner_token, _, _ = await _org_with_owner_and_media(client, db_session, "dup")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()
    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={
            "name": "Original",
            "is_default": True,
            "expected_version": template["version"],
            "elements": [{"type": "amount", "x": 5, "y": 5}],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    original = resp.json()

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates/{original['id']}/duplicate",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["id"] != original["id"]
    assert copy["name"] == "Original (copy)"
    assert copy["is_default"] is False
    assert len(copy["elements"]) == 1
    assert copy["elements"][0]["type"] == "amount"


async def test_deleting_template_unbinds_referencing_sessions(
    client: AsyncClient, db_session: AsyncSession
):
    org, owner_token, media_token, _ = await _org_with_owner_and_media(client, db_session, "delete")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()

    resp = await client.post(
        "/v1/sessions",
        json={
            "organization_id": org["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
            "display_template_id": template["id"],
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session = resp.json()
    assert session["display_template_id"] == template["id"]

    resp = await client.delete(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        f"/v1/sessions/{session['id']}/operator", headers={"Authorization": f"Bearer {media_token}"}
    )
    assert resp.json()["display_template_id"] is None


async def test_public_display_template_endpoint(client: AsyncClient, db_session: AsyncSession):
    org, owner_token, media_token, _ = await _org_with_owner_and_media(client, db_session, "public")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()
    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={
            "name": "Sunday",
            "is_default": True,
            "expected_version": template["version"],
            "elements": [{"type": "heading", "binding": {"text": "Welcome"}}],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()

    session = await create_session(client, media_token, org["id"])
    assert session["display_template_id"] == template["id"]

    resp = await client.post(
        f"/v1/sessions/{session['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    display_token = resp.json()["display_token"]

    resp = await client.get(
        f"/v1/sessions/{session['id']}/display-template/public",
        headers={"Authorization": f"Bearer {display_token}"},
    )
    assert resp.status_code == 200, resp.text
    fetched = resp.json()
    assert fetched["id"] == template["id"]
    assert fetched["name"] == "Sunday"
    assert len(fetched["elements"]) == 1
    assert fetched["elements"][0]["binding"]["text"] == "Welcome"

    # No token at all -> unauthorized, same as the WS channel and .../public.
    resp = await client.get(f"/v1/sessions/{session['id']}/display-template/public")
    assert resp.status_code == 401

    # A session with no bound template returns null, not a 404 -- absence of
    # a custom layout is a valid state the display page falls back on. Needs
    # a fresh org: this org now has a default template, and there's no way
    # to opt a session out of auto-binding the org default once one exists.
    bare_org, _, bare_media_token, _ = await _org_with_owner_and_media(
        client, db_session, "public-bare"
    )
    bare_session = await create_session(client, bare_media_token, bare_org["id"])
    resp = await client.post(
        f"/v1/sessions/{bare_session['id']}/display-token",
        headers={"Authorization": f"Bearer {bare_media_token}"},
    )
    bare_token = resp.json()["display_token"]
    resp = await client.get(
        f"/v1/sessions/{bare_session['id']}/display-template/public",
        headers={"Authorization": f"Bearer {bare_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


async def test_public_display_template_renders_qr_code_as_image(
    client: AsyncClient, db_session: AsyncSession
):
    org, owner_token, media_token, _ = await _org_with_owner_and_media(client, db_session, "qr")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()
    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={
            "name": "QR",
            "is_default": True,
            "expected_version": template["version"],
            "elements": [
                {"type": "qr_code", "binding": {"value": "https://example.org/give"}},
                # No value set -- should come back with no generated image.
                {"type": "qr_code", "binding": {}},
            ],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()

    session = await create_session(client, media_token, org["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    display_token = resp.json()["display_token"]

    resp = await client.get(
        f"/v1/sessions/{session['id']}/display-template/public",
        headers={"Authorization": f"Bearer {display_token}"},
    )
    assert resp.status_code == 200, resp.text
    elements = resp.json()["elements"]

    with_value = next(el for el in elements if el["binding"].get("value") == "https://example.org/give")
    assert with_value["binding"]["qr_data_uri"].startswith("data:image/png;base64,")

    without_value = next(el for el in elements if el["binding"].get("value") is None)
    assert "qr_data_uri" not in without_value["binding"]

    # The persisted template (fetched as an operator, not the public route)
    # never gained a stored qr_data_uri -- it's generated fresh per request.
    resp = await client.get(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    stored_elements = resp.json()["elements"]
    stored_with_value = next(
        el for el in stored_elements if el["binding"].get("value") == "https://example.org/give"
    )
    assert "qr_data_uri" not in stored_with_value["binding"]


async def test_finance_cannot_create_or_edit_templates(
    client: AsyncClient, db_session: AsyncSession
):
    org, owner_token, _, finance_token = await _org_with_owner_and_media(
        client, db_session, "financerbac"
    )

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    template = resp.json()

    resp = await client.get(
        f"/v1/organizations/{org['id']}/display-templates",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = await client.put(
        f"/v1/organizations/{org['id']}/display-templates/{template['id']}",
        json={"name": "Hack", "expected_version": template["version"], "elements": []},
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 403
