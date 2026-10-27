import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.main import api_router
from app.core.config import settings
from app.core.db import engine
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.redis import redis_client
from app.core.request_id import RequestIdMiddleware

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(
    lifespan=lifespan,
    # Interactive docs only outside staging and production; /openapi.json stays for the Orval flow.
    docs_url=None if settings.is_deployed else "/docs",
    redoc_url=None if settings.is_deployed else "/redoc",
)

register_error_handlers(app)

# Production is same-origin (the proxy serves the SPA and the API on one origin), so CORS is
# only for local development, where Vite runs on another port.
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.BACKEND_CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Requested-With", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
app.add_middleware(RequestIdMiddleware)

app.include_router(health_router)
app.include_router(api_router)
