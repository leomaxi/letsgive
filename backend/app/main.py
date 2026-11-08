import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.display_page import router as display_page_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.domain import imap_idle
from app.domain.imap_polling import poll_all_imap_connections
from app.domain.subscriptions import revert_expired_plans

logger = logging.getLogger("letsgive.imap")
subscriptions_logger = logging.getLogger("letsgive.subscriptions")
error_logger = logging.getLogger("letsgive.errors")
if not error_logger.handlers:
    # uvicorn doesn't attach handlers to app loggers (see notifications.py).
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(message)s"))
    error_logger.addHandler(_handler)
    error_logger.setLevel(logging.INFO)


async def _unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Any error no endpoint handled: log the full traceback under a short
    reference and give the user a readable message carrying that same
    reference, instead of a bare "Internal Server Error" (which the
    frontend can only show as "Request failed (500)"). Support can then
    find the exact failure with: journalctl -u letsgive-backend | grep <ref>
    """
    ref = uuid.uuid4().hex[:8].upper()
    error_logger.exception("Unhandled error ref=%s %s %s", ref, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                f"Something went wrong on our side (reference {ref}). Please try again; "
                "if it keeps happening, send this reference to support."
            ),
            "error_reference": ref,
        },
    )


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

    Sleeps *before* its first poll, deliberately -- a real production
    measurement showed a fresh process's IDLE-watcher startup sweep (which
    already does its own catch-up poll per connection, covering exactly the
    "what happened while we were down" gap this loop also exists for)
    racing this loop's old immediate first iteration for the same
    connection's lock, adding a genuine 25+ second delay to a real deposit's
    detection for no benefit -- both mechanisms were doing the same
    redundant work at the same moment right after every single restart.
    """
    interval = get_settings().imap_poll_interval_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as db:
                await poll_all_imap_connections(db)
        except Exception:  # noqa: BLE001 -- a bad poll cycle must not kill the loop
            logger.exception("IMAP poll loop iteration failed")


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
    app.add_exception_handler(Exception, _unexpected_error)
    app.include_router(api_router)
    app.include_router(display_page_router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
