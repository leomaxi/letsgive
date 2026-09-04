from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.display_page import router as display_page_router
from app.api.v1.router import api_router
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


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
