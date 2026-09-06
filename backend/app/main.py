import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.display_page import router as display_page_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.domain.imap_polling import poll_all_imap_connections

logger = logging.getLogger("letsgive.imap")

IMAP_POLL_INTERVAL_SECONDS = 60


async def _imap_poll_loop() -> None:
    """Background task: periodically checks every connected IMAP mailbox for
    new deposit-notification emails (spec 9 push vs. pull -- IMAP has no
    webhook of its own, so this app has to ask instead of being told).
    Single-process only, matching this app's other in-process infra
    (app/domain/realtime.py) -- a horizontally-scaled deployment would need
    to run this on exactly one worker, or move it to a real task queue.
    """
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await poll_all_imap_connections(db)
        except Exception:  # noqa: BLE001 -- a bad poll cycle must not kill the loop
            logger.exception("IMAP poll loop iteration failed")
        await asyncio.sleep(IMAP_POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Skipped under pytest: the test suite runs against a per-test in-memory
    # SQLite engine reached only through a dependency override, not the
    # module-level engine this loop would otherwise connect to -- starting it
    # would just poll the wrong (or nonexistent) database every 60s.
    task = None if "pytest" in sys.modules else asyncio.create_task(_imap_poll_loop())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


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
