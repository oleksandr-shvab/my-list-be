from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.email import send_password_reset_email
from app.core.exceptions import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidOrExpiredResetTokenError,
)
from app.crud.password_reset import (
    create_password_reset_token,
    delete_password_reset_token,
    get_password_reset_user_id,
)
from app.crud.session import create_session, delete_session
from app.crud.user import (
    authenticate_user,
    create_user,
    get_user_by_email,
    get_user_by_id,
    update_user_password,
)
from app.models.user import User


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
