import logging

from sqlalchemy import text

from app.core.db import engine
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


async def check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Readiness check: database is not reachable")
        return False
    return True


async def check_redis() -> bool:
    try:
        await redis_client.ping()
    except Exception:
        logger.exception("Readiness check: Redis is not reachable")
        return False
    return True


async def failed_dependencies() -> list[str]:
    checks = {"database": check_database, "redis": check_redis}
    return [name for name, check in checks.items() if not await check()]
