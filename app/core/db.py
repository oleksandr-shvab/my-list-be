from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# Stable constraint and index names, so migrations and `alembic check` never depend on
# Postgres's default names. Check constraints must be given a `name=` on the model.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Fetch server-side defaults (`updated_at` after an update) in the same statement, so reading
    # them after a flush does not trigger a lazy load, which AsyncSession cannot do.
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}


def enum_check(column: str, enum: type[StrEnum]) -> str:
    """The text of a CHECK constraint that allows exactly the values of a StrEnum."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    pool_pre_ping=True,
    connect_args={"server_settings": {"statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS)}},
)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
