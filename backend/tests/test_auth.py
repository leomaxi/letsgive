import pyotp
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_org, enable_mfa, register_and_login


async def test_register_and_login(client: AsyncClient):
    token = await register_and_login(client, "owner@example.org")
    assert token

    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "owner@example.org"
    assert resp.json()["mfa_enabled"] is False


async def test_duplicate_registration_rejected(client: AsyncClient):
    await register_and_login(client, "dup@example.org")
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "dup@example.org", "password": "another-password", "full_name": "Dup"},
    )
    assert resp.status_code == 409


async def test_user_can_change_login_email(client: AsyncClient):
    token = await register_and_login(client, "old-email@example.org")

    resp = await client.patch(
        "/v1/auth/me/email",
        json={"email": "new-email@example.org", "current_password": "correct-horse-battery"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == "new-email@example.org"

    resp = await client.post(
        "/v1/auth/login",
        json={"email": "old-email@example.org", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 401

    resp = await client.post(
        "/v1/auth/login",
        json={"email": "new-email@example.org", "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, resp.text


async def test_change_email_requires_password_and_unique_email(client: AsyncClient):
    token = await register_and_login(client, "change-email@example.org")
    await register_and_login(client, "taken-email@example.org")

    resp = await client.patch(
        "/v1/auth/me/email",
        json={"email": "unused-email@example.org", "current_password": "wrong-password"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400

    resp = await client.patch(
        "/v1/auth/me/email",
        json={"email": "taken-email@example.org", "current_password": "correct-horse-battery"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


async def test_login_wrong_password_rejected(client: AsyncClient):
    await register_and_login(client, "wrongpw@example.org")
    resp = await client.post(
        "/v1/auth/login", json={"email": "wrongpw@example.org", "password": "not-the-password"}
    )
    assert resp.status_code == 401


async def test_org_creation_requires_mfa(client: AsyncClient):
    token = await register_and_login(client, "nomfa@example.org")
    resp = await client.post(
        "/v1/organizations",
        json={"name": "No MFA Org", "country": "CA", "timezone": "UTC", "currency": "CAD"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_org_creation_succeeds_after_mfa(client: AsyncClient):
    token = await register_and_login(client, "hasmfa@example.org")
    await enable_mfa(client, token)
    org = await create_org(client, token, "Has MFA Org")
    assert org["name"] == "Has MFA Org"
    assert org["country"] == "CA"


async def test_login_requires_mfa_code_once_enabled(client: AsyncClient):
    email = "mfalogin@example.org"
    password = "correct-horse-battery"
    token = await register_and_login(client, email, password)
    await enable_mfa(client, token)

    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "mfa_required"

    resp = await client.post(
        "/v1/auth/login", json={"email": email, "password": password, "mfa_code": "000000"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "mfa_invalid"


async def test_mfa_secret_is_encrypted_at_rest(client: AsyncClient, db_session: AsyncSession):
    """The API and the ORM should both see the real plaintext secret (MFA
    keeps working normally); what's actually sitting in the database column
    should be neither that plaintext nor a trivial encoding of it, and
    should look like a Fernet token (versioned, base64) rather than mangled
    garbage -- i.e. real encryption happened, not accidental corruption.
    """
    email = "encrypted-mfa@example.org"
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/v1/auth/mfa/enroll", headers=headers)
    assert resp.status_code == 200, resp.text
    plaintext_secret = resp.json()["secret"]

    code = pyotp.TOTP(plaintext_secret).now()
    resp = await client.post("/v1/auth/mfa/activate", json={"code": code}, headers=headers)
    assert resp.status_code == 204, resp.text

    # Raw SQL bypasses the ORM's transparent decryption -- this is exactly
    # what's physically stored in the column.
    result = await db_session.execute(
        text("SELECT mfa_secret FROM users WHERE email = :email"), {"email": email}
    )
    raw_stored_value = result.scalar_one()

    assert raw_stored_value != plaintext_secret
    assert plaintext_secret not in raw_stored_value
    assert plaintext_secret.encode().hex() not in raw_stored_value
    # A Fernet token is itself urlsafe-base64 of a versioned+timestamped+
    # HMAC'd blob -- expect that shape, not e.g. the plaintext re-encoded.
    assert raw_stored_value.startswith("gAAAAA")

    # Meanwhile, going through the normal API/ORM path still works exactly
    # as if nothing were encrypted -- the whole point of a transparent type.
    resp = await client.get("/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["mfa_enabled"] is True

    login = await client.post(
        "/v1/auth/login",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "mfa_code": pyotp.TOTP(plaintext_secret).now(),
        },
    )
    assert login.status_code == 200, login.text


async def test_webhook_secret_is_encrypted_at_rest(client: AsyncClient, db_session: AsyncSession):
    token = await register_and_login(client, "webhook-owner@example.org")
    await enable_mfa(client, token)
    org = await create_org(client, token, "Webhook Secrecy Org")

    resp = await client.post(
        f"/v1/organizations/{org['id']}/connections",
        json={"provider": "fake", "mailbox": "give@church.org"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    connection_id = resp.json()["id"]

    result = await db_session.execute(
        text("SELECT webhook_secret FROM mailbox_connections WHERE id = :id"),
        {"id": connection_id},
    )
    raw_stored_value = result.scalar_one()
    assert raw_stored_value.startswith("gAAAAA")
