from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import EmailAlreadyRegisteredError, InvalidCredentialsError
from app.crud.session import create_session, delete_session
from app.crud.user import authenticate_user, create_user, get_user_by_email
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
