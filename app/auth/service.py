import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.email import send_password_reset_email
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    IncorrectPasswordError,
    InvalidCredentialsError,
    InvalidOrExpiredResetTokenError,
)
from app.core.security import verify_password
from app.crud.password_reset import (
    create_password_reset_token,
    delete_password_reset_token,
    get_password_reset_user_id,
)
from app.crud.session import (
    create_session,
    delete_session,
    revoke_all_sessions,
    revoke_other_sessions,
)
from app.crud.user import (
    authenticate_user,
    create_user,
    get_user_by_email,
    get_user_by_id,
    update_user_password,
)
from app.models.user import User

logger = logging.getLogger(__name__)


async def register(db: AsyncSession, email: str, password: str) -> tuple[User, str]:
    if await get_user_by_email(db, email) is not None:
        raise EmailAlreadyRegisteredError()
    user = await create_user(db, email=email, password=password)
    token = await create_session(user.id)
    return user, token


async def login(db: AsyncSession, email: str, password: str) -> tuple[User, str]:
    user = await authenticate_user(db, email, password)
    if user is None or not user.is_active:
        raise InvalidCredentialsError()
    token = await create_session(user.id)
    return user, token


async def logout(token: str) -> None:
    await delete_session(token)


async def request_password_reset(db: AsyncSession, email: str) -> None:
    user = await get_user_by_email(db, email)
    if user is not None and user.is_active:
        token = await create_password_reset_token(user.id)
        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={token}"
        await send_password_reset_email(user.email, reset_link)


async def reset_password(db: AsyncSession, token: str, new_password: str) -> None:
    user_id = await get_password_reset_user_id(token)
    if user_id is None:
        raise InvalidOrExpiredResetTokenError()
    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        raise InvalidOrExpiredResetTokenError()
    await update_user_password(db, user, new_password)
    await delete_password_reset_token(token, user_id)
    try:
        await revoke_all_sessions(user_id)
    except Exception:
        logger.exception("Failed to revoke sessions for user %s after password reset", user_id)


async def update_password(
    db: AsyncSession, user: User, current_token: str, old_password: str, new_password: str
) -> None:
    if not verify_password(old_password, user.hashed_password):
        raise IncorrectPasswordError()
    await update_user_password(db, user, new_password)
    try:
        await revoke_other_sessions(user.id, keep_token=current_token)
    except Exception:
        logger.exception(
            "Failed to revoke other sessions for user %s after password update", user.id
        )
