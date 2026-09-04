import asyncio
from collections.abc import Generator

import pyotp
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db.base import Base
from app.db.models.membership import Membership, MembershipStatus
from app.db.session import get_db
from app.domain.notifications import get_notifier
from app.main import app
from tests.conftest import CapturingNotifier


@pytest.fixture
def sync_client() -> Generator[tuple[TestClient, CapturingNotifier], None, None]:
    """A sync TestClient for exercising the WebSocket route, which
    httpx.AsyncClient (used by the rest of the suite) can't do. Sets up its
    own isolated in-memory DB, mirroring the async `db_session` fixture --
    websocket tests can't share an async fixture across the sync/async
    boundary that starlette.testclient.TestClient runs its portal on.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create_all())

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    notifier = CapturingNotifier()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_notifier] = lambda: notifier

    with TestClient(app) as client:
        yield client, notifier

    app.dependency_overrides.clear()
    asyncio.run(engine.dispose())


def _register_login(client: TestClient, email: str, password: str = "correct-horse-battery") -> str:
    resp = client.post(
        "/v1/auth/register", json={"email": email, "password": password, "full_name": "T"}
    )
    assert resp.status_code == 201, resp.text
    resp = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _enable_mfa(client: TestClient, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post("/v1/auth/mfa/enroll", headers=headers)
    secret = resp.json()["secret"]
    code = pyotp.TOTP(secret).now()
    resp = client.post("/v1/auth/mfa/activate", json={"code": code}, headers=headers)
    assert resp.status_code == 204, resp.text


def _activate_membership(membership_id: str) -> None:
    async def _do() -> None:
        gen = app.dependency_overrides[get_db]()
        session = await gen.__anext__()
        result = await session.execute(select(Membership).where(Membership.id == membership_id))
        m = result.scalar_one()
        m.status = MembershipStatus.ACTIVE
        await session.commit()
        await gen.aclose()

    asyncio.run(_do())


def _setup_org_with_finance_and_media(client: TestClient, suffix: str) -> dict:
    owner_token = _register_login(client, f"owner-{suffix}@example.org")
    _enable_mfa(client, owner_token)
    resp = client.post(
        "/v1/organizations",
        json={"name": f"RT Org {suffix}", "country": "CA", "timezone": "UTC", "currency": "CAD"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    org = resp.json()

    finance_token = _register_login(client, f"finance-{suffix}@example.org")
    _enable_mfa(client, finance_token)
    resp = client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": f"finance-{suffix}@example.org", "role": "finance"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    _activate_membership(resp.json()["id"])

    media_token = _register_login(client, f"media-{suffix}@example.org")
    resp = client.post(
        f"/v1/organizations/{org['id']}/members/invite",
        json={"email": f"media-{suffix}@example.org", "role": "media"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    _activate_membership(resp.json()["id"])

    return {
        "org": org,
        "owner_token": owner_token,
        "finance_token": finance_token,
        "media_token": media_token,
    }


def test_websocket_streams_state_as_session_progresses(
    sync_client: tuple[TestClient, CapturingNotifier],
):
    client, notifier = sync_client
    ctx = _setup_org_with_finance_and_media(client, "stream")
    media_token = ctx["media_token"]

    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session = resp.json()

    resp = client.post(
        f"/v1/sessions/{session['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    display_token = resp.json()["display_token"]

    with client.websocket_connect(
        f"/v1/sessions/{session['id']}/live?token={display_token}"
    ) as ws:
        initial = ws.receive_json()
        assert initial["status"] == "draft"
        assert initial["contribution_count"] == 0
        assert initial["organization_name"] == ctx["org"]["name"]

        resp = client.post(
            f"/v1/sessions/{session['id']}/request-approval",
            headers={"Authorization": f"Bearer {media_token}"},
        )
        approval_id = resp.json()["approval_id"]
        code = notifier.latest_code_for(session["id"])

        resp = client.post(
            f"/v1/sessions/{session['id']}/verify",
            json={"approval_id": approval_id, "code": code},
            headers={"Authorization": f"Bearer {media_token}"},
        )
        authorized = resp.json()
        assert resp.status_code == 200

        after_verify = ws.receive_json()
        assert after_verify["status"] == "authorized"

        resp = client.post(
            f"/v1/sessions/{session['id']}/start",
            json={"expected_version": authorized["version"]},
            headers={"Authorization": f"Bearer {media_token}"},
        )
        assert resp.status_code == 200

        after_start = ws.receive_json()
        assert after_start["status"] == "live"
        assert after_start["ends_at"] is not None


def test_websocket_rejects_missing_or_wrong_scope_token(
    sync_client: tuple[TestClient, CapturingNotifier],
):
    client, _ = sync_client
    ctx = _setup_org_with_finance_and_media(client, "reject")
    media_token = ctx["media_token"]

    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session_a = resp.json()
    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session_b = resp.json()

    resp = client.post(
        f"/v1/sessions/{session_a['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    token_a = resp.json()["display_token"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/sessions/{session_a['id']}/live"):
            pass

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/sessions/{session_b['id']}/live?token={token_a}"):
            pass


def test_operator_websocket_streams_fuller_state_and_requires_membership(
    sync_client: tuple[TestClient, CapturingNotifier],
):
    client, notifier = sync_client
    ctx = _setup_org_with_finance_and_media(client, "opstream")
    media_token = ctx["media_token"]

    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session = resp.json()

    resp = client.post(
        f"/v1/sessions/{session['id']}/operator-socket-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    assert resp.status_code == 200, resp.text
    op_token = resp.json()["operator_socket_token"]

    with client.websocket_connect(
        f"/v1/sessions/{session['id']}/live-operator?token={op_token}"
    ) as ws:
        initial = ws.receive_json()
        assert initial["status"] == "draft"
        # Fields a display-token client never gets, unlike the public channel.
        assert initial["version"] == session["version"]
        assert "operator_warning" in initial

        resp = client.post(
            f"/v1/sessions/{session['id']}/request-approval",
            headers={"Authorization": f"Bearer {media_token}"},
        )
        approval_id = resp.json()["approval_id"]
        code = notifier.latest_code_for(session["id"])

        resp = client.post(
            f"/v1/sessions/{session['id']}/verify",
            json={"approval_id": approval_id, "code": code},
            headers={"Authorization": f"Bearer {media_token}"},
        )
        assert resp.status_code == 200

        after_verify = ws.receive_json()
        assert after_verify["status"] == "authorized"

    # An Auditor -- a real, active member, just not Media/Finance -- can't
    # mint an operator socket token for this session.
    auditor_token = _register_login(client, "auditor-opstream@example.org")
    resp = client.post(
        f"/v1/organizations/{ctx['org']['id']}/members/invite",
        json={"email": "auditor-opstream@example.org", "role": "auditor"},
        headers={"Authorization": f"Bearer {ctx['owner_token']}"},
    )
    _activate_membership(resp.json()["id"])
    resp = client.post(
        f"/v1/sessions/{session['id']}/operator-socket-token",
        headers={"Authorization": f"Bearer {auditor_token}"},
    )
    assert resp.status_code == 403


def test_operator_websocket_rejects_missing_or_wrong_scope_token(
    sync_client: tuple[TestClient, CapturingNotifier],
):
    client, _ = sync_client
    ctx = _setup_org_with_finance_and_media(client, "opreject")
    media_token = ctx["media_token"]

    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session_a = resp.json()
    resp = client.post(
        "/v1/sessions",
        json={
            "organization_id": ctx["org"]["id"],
            "contribution_method": "e-transfer",
            "duration_seconds": 600,
        },
        headers={"Authorization": f"Bearer {media_token}"},
    )
    session_b = resp.json()

    resp = client.post(
        f"/v1/sessions/{session_a['id']}/operator-socket-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    token_a = resp.json()["operator_socket_token"]

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/v1/sessions/{session_a['id']}/live-operator"):
            pass

    # A display token (different `typ` claim) isn't accepted here either.
    resp = client.post(
        f"/v1/sessions/{session_a['id']}/display-token",
        headers={"Authorization": f"Bearer {media_token}"},
    )
    display_token = resp.json()["display_token"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/sessions/{session_a['id']}/live-operator?token={display_token}"
        ):
            pass

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/v1/sessions/{session_b['id']}/live-operator?token={token_a}"
        ):
            pass
