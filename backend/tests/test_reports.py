from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

from httpx import AsyncClient
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.contribution_event import ContributionEvent
from app.db.models.organization import Organization
from app.db.models.plan import Plan

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


async def _set_org_plan(db_session: AsyncSession, org_id: str, plan_key: str) -> None:
    plan_result = await db_session.execute(select(Plan).where(Plan.key == plan_key))
    plan = plan_result.scalar_one()
    org_result = await db_session.execute(select(Organization).where(Organization.id == org_id))
    org = org_result.scalar_one()
    org.plan_id = plan.id
    await db_session.commit()


def _raw_received_money_email(
    *, message_id: str, date: datetime, sender: str, amount: str, transfer_message: str, reference: str
) -> bytes:
    msg = EmailMessage()
    msg["From"] = f"{sender} <notify@payments.interac.ca>"
    msg["To"] = "methodistchurchstjohns@gmail.com"
    msg["Subject"] = f"Interac e-Transfer: You've received ${amount}"
    msg["Message-ID"] = message_id
    msg["Date"] = date.strftime("%a, %d %b %Y %H:%M:%S +0000")
    msg.set_content(
        "Funds Deposited!\n"
        f"${amount}\n"
        "Your funds have been automatically deposited into your account.\n\n"
        "Transfer Details\n"
        "Message:\n"
        f"{transfer_message}\n\n"
        "Reference Number:\n"
        f"{reference}\n\n"
        "Sent From:\n"
        f"{sender}\n\n"
        "Amount:\n"
        f"${amount} (CAD)"
    )
    return bytes(msg)


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
    await _set_org_plan(db_session, org["id"], "growth")
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
    await _set_org_plan(db_session, org["id"], "growth")
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

    result = await db_session.execute(
        select(ContributionEvent).where(ContributionEvent.provider_message_id == "interac-received-money")
    )
    event = result.scalar_one()
    event.export_details = None
    await db_session.commit()

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


async def test_imap_export_reads_mailbox_date_range_without_sessions(
    client: AsyncClient, db_session: AsyncSession
):
    owner_token = await register_and_login(client, "owner-mailbox-export@example.org")
    await enable_mfa(client, owner_token)
    org = await create_org(client, owner_token, "Mailbox Export Org")
    await _set_org_plan(db_session, org["id"], "growth")
    finance_token = await add_active_member(
        client,
        db_session,
        owner_token,
        org["id"],
        "finance-mailbox-export@example.org",
        "finance",
        needs_mfa=True,
    )

    creation_mock = MagicMock()
    creation_mock.login.return_value = ("OK", [b"done"])
    creation_mock.status.return_value = ("OK", [b"INBOX (UIDNEXT 99)"])
    with patch("app.domain.imap_provider.imaplib.IMAP4_SSL", return_value=creation_mock):
        resp = await client.post(
            f"/v1/organizations/{org['id']}/connections",
            json={"provider": "imap", "mailbox": "deposits@gmail.com", "imap_password": "app-pw"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
    assert resp.status_code == 201, resp.text
    connection = resp.json()

    await create_parser_profile(client, owner_token, org["id"], sender_patterns=["@payments.interac.ca"])

    in_range = datetime(2026, 9, 13, 12, 16, 35, tzinfo=timezone.utc)
    out_of_range = datetime(2026, 9, 5, 12, 16, 35, tzinfo=timezone.utc)
    mailbox_messages = {
        b"1": _raw_received_money_email(
            message_id="<in-range@payments.interac.ca>",
            date=in_range,
            sender="JAMES KINGSLEY OWUSU",
            amount="120.00",
            transfer_message="046",
            reference="C1AKjHSd4MYz",
        ),
        b"2": _raw_received_money_email(
            message_id="<old@payments.interac.ca>",
            date=out_of_range,
            sender="OLDER SENDER",
            amount="5.00",
            transfer_message="old",
            reference="OLDREF123",
        ),
    }
    export_mock = MagicMock()

    def uid_side_effect(command, *args):
        if command == "search":
            return ("OK", [b"1 2"])
        uid = args[0]
        return ("OK", [(b"BODY", mailbox_messages[uid if isinstance(uid, bytes) else uid.encode()]), b")"])

    export_mock.uid.side_effect = uid_side_effect
    with patch("app.domain.imap_polling.imaplib.IMAP4_SSL", return_value=export_mock):
        resp = await client.get(
            f"/v1/organizations/{org['id']}/contribution-export.csv",
            params={
                "connection_id": connection["id"],
                "from_datetime": datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc).isoformat(),
                "to_datetime": datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc).isoformat(),
            },
            headers={"Authorization": f"Bearer {finance_token}"},
        )

    assert resp.status_code == 200, resp.text
    csv_text = resp.text
    assert "JAMES KINGSLEY OWUSU" in csv_text
    assert "120.00" in csv_text
    assert "046" in csv_text
    assert "C1AKjHSd4MYz" in csv_text
    assert "OLDER SENDER" not in csv_text
