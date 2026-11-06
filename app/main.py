import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.main import api_router
from app.core.config import settings
from app.core.csrf import CsrfOriginMiddleware
from app.core.db import engine
from app.core.errors import ERROR_RESPONSES, register_error_handlers
from app.core.logging import configure_logging
from app.core.openapi import operation_id
from app.core.redis import redis_client
from app.core.request_id import RequestIdMiddleware
from app.media.storage import build_media_storage

configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Built once, off the event loop, so a broken storage setting stops the start.
    await anyio.to_thread.run_sync(build_media_storage)
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="Daily Game Challenge API",
    lifespan=lifespan,
    generate_unique_id_function=operation_id,
    responses=ERROR_RESPONSES,
    # No interactive docs when deployed; /openapi.json stays for the Orval flow.
    docs_url=None if settings.is_deployed else "/docs",
    redoc_url=None if settings.is_deployed else "/redoc",
)

register_error_handlers(app)

# Added first, so it is innermost: its 403 and 415 still get X-Request-ID.
app.add_middleware(CsrfOriginMiddleware)

# Production is same-origin (the proxy serves the SPA and the API), so CORS is only for local
# development, where Vite runs on another port.
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

# Health checks sit outside the versioned API.
app.include_router(health_router)
app.include_router(api_router, prefix="/v1")
