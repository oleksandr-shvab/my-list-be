import datetime as dt
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.clock import get_now
from app.core.config import serialize_origin, settings

# Point the app at the test services before any module opens a connection. Refuse to run if they
# match the dev ones: the suite drops tables and flushes Redis.
assert settings.TEST_DATABASE_URL != settings.DATABASE_URL, (
    "TEST_DATABASE_URL must differ from DATABASE_URL"
)
assert settings.TEST_REDIS_URL != settings.REDIS_URL, "TEST_REDIS_URL must differ from REDIS_URL"
settings.DATABASE_URL = settings.TEST_DATABASE_URL
settings.REDIS_URL = settings.TEST_REDIS_URL

from app.core.db import Base, get_db
from app.core.redis import redis_client
from app.main import app
from app.media.storage import get_media_storage
from tests.factories import make_local_storage

TEST_ORIGIN = serialize_origin(settings.FRONTEND_URL)
assert TEST_ORIGIN is not None


def _maintenance_url_and_db_name(database_url: str) -> tuple[str, str]:
    parsed = urlsplit(database_url.replace("postgresql+asyncpg://", "postgresql://"))
    db_name = parsed.path.lstrip("/")
    maintenance_url = urlunsplit(parsed._replace(path="/postgres"))
    return maintenance_url, db_name


async def _ensure_database_exists(database_url: str) -> None:
    maintenance_url, db_name = _maintenance_url_and_db_name(database_url)
    conn = await asyncpg.connect(maintenance_url)
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    await _ensure_database_exists(settings.TEST_DATABASE_URL)

    test_engine = create_async_engine(settings.TEST_DATABASE_URL)
    async with test_engine.begin() as conn:
        # `create_all` skips migrations; create the extension the trigram indexes need (AC-23).
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

    yield test_engine

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def client(db_session: AsyncSession, tmp_path: Path) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    # Use a storage of the tests' own, not the developer's `.env`.
    storage = make_local_storage(tmp_path)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_media_storage] = lambda: storage
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"Origin": TEST_ORIGIN}
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


SetNow = Callable[[dt.datetime], None]


@pytest.fixture
def set_now() -> Generator[SetNow, None, None]:
    """Fix the server clock. Teardown removes the override, so a test without `client` cannot
    leave it frozen."""

    def _set_now(now: dt.datetime) -> None:
        app.dependency_overrides[get_now] = lambda: now

    yield _set_now
    app.dependency_overrides.pop(get_now, None)


@pytest.fixture(autouse=True)
async def clean_redis() -> AsyncGenerator[None, None]:
    yield
    # Safe: the client points at TEST_REDIS_URL, not the dev Redis.
    await redis_client.flushdb()
