from collections.abc import AsyncGenerator
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import MetaData, func, select
from sqlalchemy.exc import DBAPIError
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


async def set_local(db: AsyncSession, name: str, value: str) -> None:
    """Set a Postgres setting for the current transaction only: `set_config` with `is_local` true,
    which this function always passes, so the value never outlives the transaction on a pooled
    connection."""
    await db.execute(select(func.set_config(name, value, True)))


# SQLSTATE for "lock_timeout expired" (`lock_not_available`).
LOCK_NOT_AVAILABLE = "55P03"


def is_lock_timeout(error: DBAPIError) -> bool:
    """Whether a database error is Postgres giving up waiting for a lock (`lock_timeout`)."""
    return getattr(error.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE


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
