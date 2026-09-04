from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Import models so they register on Base.metadata before create_all.
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models.plan import SEED_PLANS, Plan
from app.db.session import get_db
from app.domain.mailbox_providers import get_fake_provider
from app.domain.notifications import get_notifier
from app.domain.rate_limit import login_ip_rate_limiter
from app.main import app


class CapturingNotifier:
    """Test double that records OTP codes instead of sending them anywhere."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_otp(self, *, to_email: str, code: str, session_id: str) -> None:
        self.sent.append({"to_email": to_email, "code": code, "session_id": session_id})

    def latest_code_for(self, session_id: str) -> str:
        for entry in reversed(self.sent):
            if entry["session_id"] == session_id:
                return entry["code"]
        raise AssertionError(f"No OTP was sent for session {session_id}")


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    # create_all only applies schema (DDL) -- the migration's plan-seeding
    # data isn't part of it, so seed it here too or entitlement checks
    # silently no-op for the rest of the suite (org.plan_id stays None).
    async with session_factory() as seed_session:
        for plan in SEED_PLANS:
            seed_session.add(Plan(**plan))
        await seed_session.commit()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with session_factory() as session:
        yield session

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture
def notifier(db_session: AsyncSession) -> CapturingNotifier:
    capturing = CapturingNotifier()
    app.dependency_overrides[get_notifier] = lambda: capturing
    return capturing


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, notifier: CapturingNotifier
) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_fake_mailbox_provider():
    get_fake_provider().clear()
    yield
    get_fake_provider().clear()


@pytest.fixture(autouse=True)
def _reset_login_ip_rate_limiter():
    # login_ip_rate_limiter is a module-level singleton keyed by client IP,
    # and every test client shares the same ASGITransport-reported IP -- so
    # without resetting it here, unrelated tests would accumulate against
    # the same counter and eventually start failing each other with 429s.
    login_ip_rate_limiter.clear_all()
    yield
    login_ip_rate_limiter.clear_all()
