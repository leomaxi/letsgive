import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.display_page import router as display_page_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.domain import imap_idle
from app.domain.imap_polling import poll_all_imap_connections
from app.domain.subscriptions import revert_expired_plans

logger = logging.getLogger("letsgive.imap")
subscriptions_logger = logging.getLogger("letsgive.subscriptions")


async def _imap_poll_loop() -> None:
    """Background task: a slower safety net, not the primary mechanism --
    real-time detection now comes from per-mailbox IMAP IDLE watchers
    (app/domain/imap_idle.py, started at startup below and on each new
    connection). This loop still exists as defense in depth for a watcher
    task that silently died and didn't restart, and as a backstop for any
    mailbox provider that doesn't support IDLE. Single-process only,
    matching this app's other in-process infra (app/domain/realtime.py) --
    a horizontally-scaled deployment would need to run this on exactly one
    worker, or move it to a real task queue.
    """
    interval = get_settings().imap_poll_interval_seconds
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await poll_all_imap_connections(db)
        except Exception:  # noqa: BLE001 -- a bad poll cycle must not kill the loop
            logger.exception("IMAP poll loop iteration failed")
        await asyncio.sleep(interval)


async def _plan_expiry_loop() -> None:
    """Background task: reverts any organization whose admin-assigned
    plan_expires_at has passed back to the Starter plan. Same shape as
    _imap_poll_loop above -- single-process, tolerant of a bad iteration.
    """
    interval = get_settings().plan_expiry_check_interval_seconds
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await revert_expired_plans(db)
        except Exception:  # noqa: BLE001 -- a bad cycle must not kill the loop
            subscriptions_logger.exception("Plan expiry loop iteration failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Skipped under pytest: the test suite runs against a per-test in-memory
    # SQLite engine reached only through a dependency override, not the
    # module-level engine these loops would otherwise connect to -- starting
    # them would just poll the wrong (or nonexistent) database.
    tasks: list[asyncio.Task] = []
    if "pytest" not in sys.modules:
        tasks = [asyncio.create_task(_imap_poll_loop()), asyncio.create_task(_plan_expiry_loop())]
        async with AsyncSessionLocal() as db:
            await imap_idle.start_all_watchers(db)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        imap_idle.stop_all_watchers()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Let's Give API",
        version="0.1.0",
        lifespan=lifespan,
        debug=settings.env == "development",
    )
    app.include_router(api_router)
    app.include_router(display_page_router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
