import secrets
import uuid

from app.core.config import settings
from app.core.redis import redis_client

SESSION_KEY_PREFIX = "session:"
SESSION_USER_KEY_PREFIX = "sessions_user:"


async def create_session(user_id: uuid.UUID) -> str:
    token = secrets.token_urlsafe(32)
    ttl = settings.SESSION_EXPIRE_MINUTES * 60
    user_sessions_key = f"{SESSION_USER_KEY_PREFIX}{user_id}"
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.set(f"{SESSION_KEY_PREFIX}{token}", str(user_id), ex=ttl)
        pipe.sadd(user_sessions_key, token)
        pipe.expire(user_sessions_key, ttl)
        await pipe.execute()
    return token


async def get_session(token: str) -> uuid.UUID | None:
    user_id = await redis_client.get(f"{SESSION_KEY_PREFIX}{token}")
    if user_id is None:
        return None
    return uuid.UUID(user_id)


async def delete_session(token: str) -> None:
    user_id = await get_session(token)
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.delete(f"{SESSION_KEY_PREFIX}{token}")
        if user_id is not None:
            pipe.srem(f"{SESSION_USER_KEY_PREFIX}{user_id}", token)
        await pipe.execute()


async def revoke_other_sessions(user_id: uuid.UUID, keep_token: str) -> None:
    user_sessions_key = f"{SESSION_USER_KEY_PREFIX}{user_id}"
    tokens = await redis_client.smembers(user_sessions_key)
    other_tokens = [token for token in tokens if token != keep_token]
    if not other_tokens:
        return
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.delete(*(f"{SESSION_KEY_PREFIX}{token}" for token in other_tokens))
        pipe.srem(user_sessions_key, *other_tokens)
        await pipe.execute()


async def revoke_all_sessions(user_id: uuid.UUID) -> None:
    user_sessions_key = f"{SESSION_USER_KEY_PREFIX}{user_id}"
    tokens = await redis_client.smembers(user_sessions_key)
    async with redis_client.pipeline(transaction=True) as pipe:
        if tokens:
            pipe.delete(*(f"{SESSION_KEY_PREFIX}{token}" for token in tokens))
        pipe.delete(user_sessions_key)
        await pipe.execute()
