from collections.abc import AsyncGenerator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import settings
from app.core.db import Base, get_db
from app.core.redis import redis_client
from app.main import app


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
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def clean_redis() -> AsyncGenerator[None, None]:
    yield
    async for key in redis_client.scan_iter(match="password_reset*"):
        await redis_client.delete(key)
    async for key in redis_client.scan_iter(match="session:*"):
        await redis_client.delete(key)
