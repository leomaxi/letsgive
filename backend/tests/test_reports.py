from io import BytesIO

from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import CapturingNotifier
from tests.helpers import (
    add_active_member,
    approve_session,
    create_fake_connection,
    create_org,
    create_session,
    enable_mfa,
    register_and_login,
)


async def _org_with_live_session(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier, suffix: str
):
    owner_token = await register_and_login(client, f"owner-pdf-{suffix}@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, f"PDF Org {suffix}")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"media-pdf-{suffix}@example.org", "media"
    )
    finance_token = await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        f"finance-pdf-{suffix}@example.org",
        "finance",
        needs_mfa=True,
    )
    auditor_token = await add_active_member(
        client, db_session, owner_token, org["id"], f"auditor-pdf-{suffix}@example.org", "auditor"
    )

    connection = await create_fake_connection(
        client, owner_token, org["id"], mailbox=f"give-{suffix}@church.org"
    )
    session = await create_session(
        client, media_token, org["id"], mailbox_connection_id=connection["id"], test_mode=True
    )
    session = await approve_session(client, notifier, media_token, session["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": session["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/v1/sessions/{session['id']}/simulate-deposit",
        json={"amount": "25.00"},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 201, resp.text

    return {
        "org": org,
        "session": session,
        "owner_token": owner_token,
        "media_token": media_token,
        "finance_token": finance_token,
        "auditor_token": auditor_token,
    }


async def test_pdf_export_returns_a_real_pdf(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _org_with_live_session(client, db_session, notifier, "valid")

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}/export.pdf",
        headers={"Authorization": f"Bearer {ctx['owner_token']}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/pdf"
    assert "session-" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].endswith('.pdf"')

    body = resp.content
    # A real, complete PDF document -- not just "some bytes that happen to
    # come back with a PDF content-type."
    assert body.startswith(b"%PDF-")
    assert b"%%EOF" in body[-64:]

    # Parse it back and confirm this session's *actual* data made it onto
    # the page -- not just that some valid-looking PDF came back. Page
    # content streams are FlateDecode-compressed by default, so a raw
    # byte/substring search over `body` would never find this text even in
    # a correctly-rendered PDF; extracting it properly is the only way to
    # tell a correct render apart from an empty or generic one.
    text = PdfReader(BytesIO(body)).pages[0].extract_text()
    assert ctx["org"]["name"] in text
    assert ctx["session"]["id"] in text
    assert "1" in text  # validated_count
    assert "25" in text  # the simulated deposit amount
    assert "connected" in text.lower()  # the bound mailbox connection's status


async def test_pdf_export_allowed_for_finance_auditor_owner_not_media(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _org_with_live_session(client, db_session, notifier, "rbac")

    for token in (ctx["owner_token"], ctx["finance_token"], ctx["auditor_token"]):
        resp = await client.get(
            f"/v1/reports/sessions/{ctx['session']['id']}/export.pdf",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}/export.pdf",
        headers={"Authorization": f"Bearer {ctx['media_token']}"},
    )
    assert resp.status_code == 403, resp.text


async def test_pdf_export_for_outsider_returns_404(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    ctx = await _org_with_live_session(client, db_session, notifier, "outsider")
    outsider_token = await register_and_login(client, "outsider-pdf@example.org")

    resp = await client.get(
        f"/v1/reports/sessions/{ctx['session']['id']}/export.pdf",
        headers={"Authorization": f"Bearer {outsider_token}"},
    )
    assert resp.status_code == 404, resp.text
