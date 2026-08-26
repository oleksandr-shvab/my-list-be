import secrets
import uuid

from app.core.config import settings
from app.core.redis import redis_client

SESSION_KEY_PREFIX = "session:"


async def create_session(user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    await redis_client.set(
        f"{SESSION_KEY_PREFIX}{token}",
        str(user_id),
        ex=settings.SESSION_EXPIRE_MINUTES * 60,
    )
    return token


async def get_session(token: str) -> uuid.UUID | None:
    user_id = await redis_client.get(f"{SESSION_KEY_PREFIX}{token}")
    if user_id is None:
        return None
    return uuid.UUID(user_id)


async def delete_session(token: str) -> None:
    await redis_client.delete(f"{SESSION_KEY_PREFIX}{token}")
