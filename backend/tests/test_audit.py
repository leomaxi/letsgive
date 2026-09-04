from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog
from tests.helpers import create_org, enable_mfa, register_and_login


async def test_organization_creation_writes_audit_event(client: AsyncClient, db_session: AsyncSession):
    token = await register_and_login(client, "audited-owner@example.org")
    await enable_mfa(client, token)
    org = await create_org(client, token, "Audited Org")

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.organization_id == org["id"], AuditLog.action == "organization.created"
        )
    )
    entries = result.scalars().all()
    assert len(entries) == 1
    assert entries[0].after["name"] == "Audited Org"
