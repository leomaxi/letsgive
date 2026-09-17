from io import BytesIO
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import CapturingNotifier
from tests.helpers import (
    add_active_member,
    approve_session,
    create_fake_connection,
    create_org,
    create_parser_profile,
    create_session,
    deliver_webhook,
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


async def test_owner_can_export_contributions_for_period_with_keywords_and_template(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    owner_token = await register_and_login(client, "owner-export@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Export Org")
    finance_token = await add_active_member(
        client, db_session, owner_token, org["id"], "finance-export@example.org", "finance", needs_mfa=True
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-export@example.org", "media"
    )
    connection = await create_fake_connection(client, owner_token, org["id"])
    await create_parser_profile(client, owner_token, org["id"], sender_patterns=["notifications@fakebank.com"])
    session = await create_session(client, media_token, org["id"], mailbox_connection_id=connection["id"])
    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    now = datetime.now(timezone.utc)
    for message_id, body in [
        ("export-tithe", "You have received $25.00 CAD. Message: tithe September. Reference ABC12345."),
        ("export-building", "You have received $40.00 CAD. Message: building fund. Reference DEF67890."),
    ]:
        resp = await deliver_webhook(
            client,
            db_session,
            connection["id"],
            provider_message_id=message_id,
            sender='"Ada Lovelace" <notifications@fakebank.com>',
            subject="Deposit received",
            body=body,
            received_at=now,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["decision"] == "accepted"

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export/fields/sample",
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    assert {field["key"] for field in resp.json()} >= {"sender_name", "message", "reference_number"}

    resp = await client.post(
        f"/v1/organizations/{org['id']}/contribution-export/templates",
        json={
            "name": "Accounting",
            "field_keys": ["sender_name", "received_at", "amount", "message", "reference_number"],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text
    template = resp.json()

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export.csv",
        params={
            "connection_id": connection["id"],
            "from_datetime": (now - timedelta(minutes=5)).isoformat(),
            "to_datetime": (now + timedelta(minutes=5)).isoformat(),
            "keywords": "tithe, missions",
            "template_id": template["id"],
        },
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    csv_text = resp.text
    assert "Sender's name - Sent from,Date time,Amount,Message,Reference Number" in csv_text
    assert "25.00" in csv_text
    assert "ABC12345" in csv_text
    assert "40.00" not in csv_text

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export.csv",
        params={
            "from_datetime": (now - timedelta(minutes=5)).isoformat(),
            "to_datetime": (now + timedelta(minutes=5)).isoformat(),
        },
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 422, resp.text


async def test_media_cannot_manage_or_run_contribution_export(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-export-rbac@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Export RBAC Org")
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-export-rbac@example.org", "media"
    )

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export/templates",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 403, resp.text


async def test_received_money_export_uses_interac_sent_from_as_sender_name(
    client: AsyncClient, db_session: AsyncSession, notifier: CapturingNotifier
):
    owner_token = await register_and_login(client, "owner-interac-export@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Interac Export Org")
    finance_token = await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        "finance-interac-export@example.org",
        "finance",
        needs_mfa=True,
    )
    media_token = await add_active_member(
        client, db_session, owner_token, org["id"], "media-interac-export@example.org", "media"
    )
    connection = await create_fake_connection(client, owner_token, org["id"])
    await create_parser_profile(client, owner_token, org["id"], sender_patterns=["@payments.interac.ca"])
    session = await create_session(client, media_token, org["id"], mailbox_connection_id=connection["id"])
    authorized = await approve_session(client, notifier, media_token, session["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/start",
        json={"expected_version": authorized["version"]},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text

    now = datetime.now(timezone.utc)
    resp = await deliver_webhook(
        client,
        db_session,
        connection["id"],
        provider_message_id="interac-received-money",
        sender="JAMES KINGSLEY OWUSU <notify@payments.interac.ca>",
        subject="Funds Deposited",
        body=(
            "Hi ST. JOHN'S METHODIST CHURCH,\n"
            "Funds Deposited!\n"
            "$120.00\n"
            "Your funds have been automatically deposited into your account at Scotiabank.\n\n"
            "Transfer Details\n"
            "Message:\n"
            "046\n\n"
            "Date:\n"
            "Sept 13, 2026\n\n"
            "Reference Number:\n"
            "C1AKjHSd4MYz\n\n"
            "Sent From:\n"
            "JAMES KINGSLEY OWUSU\n\n"
            "Amount:\n"
            "$120.00 (CAD)"
        ),
        received_at=now,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "accepted"

    resp = await client.get(
        f"/v1/organizations/{org['id']}/contribution-export.csv",
        params={
            "connection_id": connection["id"],
            "from_datetime": (now - timedelta(minutes=5)).isoformat(),
            "to_datetime": (now + timedelta(minutes=5)).isoformat(),
        },
        headers={"Authorization": f"Bearer {finance_token}"},
    )
    assert resp.status_code == 200, resp.text
    csv_text = resp.text
    assert "JAMES KINGSLEY OWUSU" in csv_text
    assert "046" in csv_text
    assert "C1AKjHSd4MYz" in csv_text
