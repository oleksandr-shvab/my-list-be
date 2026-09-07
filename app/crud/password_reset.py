import secrets
import uuid

from app.core.redis import redis_client

PASSWORD_RESET_KEY_PREFIX = "password_reset:"
PASSWORD_RESET_USER_KEY_PREFIX = "password_reset_user:"
PASSWORD_RESET_EXPIRE_SECONDS = 60 * 60 * 24 * 2


async def create_password_reset_token(user_id: uuid.UUID) -> str:
    existing_token = await redis_client.get(f"{PASSWORD_RESET_USER_KEY_PREFIX}{user_id}")
    if existing_token is not None:
        await redis_client.delete(f"{PASSWORD_RESET_KEY_PREFIX}{existing_token}")

    token = secrets.token_urlsafe(32)
    await redis_client.set(
        f"{PASSWORD_RESET_KEY_PREFIX}{token}",
        str(user_id),
        ex=PASSWORD_RESET_EXPIRE_SECONDS,
    )
    await redis_client.set(
        f"{PASSWORD_RESET_USER_KEY_PREFIX}{user_id}",
        token,
        ex=PASSWORD_RESET_EXPIRE_SECONDS,
    )
    return token


async def get_password_reset_user_id(token: str) -> uuid.UUID | None:
    user_id = await redis_client.get(f"{PASSWORD_RESET_KEY_PREFIX}{token}")
    if user_id is None:
        return None
    return uuid.UUID(user_id)


async def delete_password_reset_token(token: str, user_id: uuid.UUID) -> None:
    await redis_client.delete(f"{PASSWORD_RESET_KEY_PREFIX}{token}")
    await redis_client.delete(f"{PASSWORD_RESET_USER_KEY_PREFIX}{user_id}")
