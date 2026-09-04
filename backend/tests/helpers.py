import json
from datetime import datetime

import pyotp
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.mailbox_connection import MailboxConnection
from app.db.models.membership import Membership, MembershipStatus
from app.domain.mailbox_providers import RawMessage, get_fake_provider, sign_webhook_body


async def register_and_login(
    client: AsyncClient, email: str, password: str = "correct-horse-battery"
) -> str:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Test User"},
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def enable_mfa(client: AsyncClient, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/v1/auth/mfa/enroll", headers=headers)
    assert resp.status_code == 200, resp.text
    secret = resp.json()["secret"]

    code = pyotp.TOTP(secret).now()
    resp = await client.post("/v1/auth/mfa/activate", json={"code": code}, headers=headers)
    assert resp.status_code == 204, resp.text


async def create_org(client: AsyncClient, token: str, name: str = "First Church") -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(
        "/v1/organizations",
        json={
            "name": name,
            "country": "CA",
            "timezone": "America/Toronto",
            "currency": "CAD",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def add_active_member(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_token: str,
    org_id: str,
    email: str,
    role: str,
    password: str = "correct-horse-battery",
    needs_mfa: bool = False,
) -> str:
    """Registers a user, has the owner invite them, and activates the membership
    directly against the DB rather than through POST /v1/me/invitations/{id}/accept
    -- most callers don't care about the accept step itself, just an active member
    to test something else with. Returns their access token.
    """
    token = await register_and_login(client, email, password)
    if needs_mfa:
        await enable_mfa(client, token)

    resp = await client.post(
        f"/v1/organizations/{org_id}/members/invite",
        json={"email": email, "role": role},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert resp.status_code == 201, resp.text

    result = await db_session.execute(select(Membership).where(Membership.id == resp.json()["id"]))
    membership = result.scalar_one()
    membership.status = MembershipStatus.ACTIVE
    await db_session.commit()

    return token


async def create_session(
    client: AsyncClient,
    token: str,
    organization_id: str,
    duration_seconds: int = 1800,
    contribution_method: str = "e-transfer",
    mailbox_connection_id: str | None = None,
    test_mode: bool = False,
) -> dict:
    body = {
        "organization_id": organization_id,
        "contribution_method": contribution_method,
        "duration_seconds": duration_seconds,
        "test_mode": test_mode,
    }
    if mailbox_connection_id is not None:
        body["mailbox_connection_id"] = mailbox_connection_id
    resp = await client.post(
        "/v1/sessions",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def approve_session(client: AsyncClient, notifier, media_token: str, session_id: str) -> dict:
    """Runs request-approval + verify using the captured OTP code. Returns the
    authorized session's operator state.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/request-approval",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    approval_id = resp.json()["approval_id"]
    code = notifier.latest_code_for(session_id)

    resp = await client.post(
        f"/v1/sessions/{session_id}/verify",
        json={"approval_id": approval_id, "code": code},
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def create_fake_connection(
    client: AsyncClient, token: str, organization_id: str, mailbox: str = "finance@church.org"
) -> dict:
    resp = await client.post(
        f"/v1/organizations/{organization_id}/connections",
        json={"provider": "fake", "mailbox": mailbox},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_parser_profile(
    client: AsyncClient,
    token: str,
    organization_id: str,
    sender_patterns: list[str],
    **overrides,
) -> dict:
    body = {"name": "Default Bank", "sender_patterns": sender_patterns, **overrides}
    resp = await client.post(
        f"/v1/organizations/{organization_id}/parser-profiles",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def deliver_webhook(
    client: AsyncClient,
    db_session: AsyncSession,
    connection_id: str,
    provider_message_id: str,
    sender: str,
    subject: str,
    body: str,
    received_at: datetime,
):
    result = await db_session.execute(
        select(MailboxConnection).where(MailboxConnection.id == connection_id)
    )
    connection = result.scalar_one()

    get_fake_provider().seed_message(
        connection_id,
        RawMessage(
            provider_message_id=provider_message_id,
            sender=sender,
            subject=subject,
            body=body,
            received_at=received_at,
        ),
    )

    raw_body = json.dumps(
        {"connection_id": connection_id, "provider_message_id": provider_message_id}
    ).encode()
    signature = sign_webhook_body(connection.webhook_secret, raw_body)

    return await client.post(
        "/v1/providers/fake/webhook",
        content=raw_body,
        headers={"Content-Type": "application/json", "X-LetsGive-Signature": signature},
    )
