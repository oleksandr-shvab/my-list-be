from typing import Annotated

from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import NotAuthenticatedError
from app.crud.session import get_session
from app.crud.user import get_user_by_id
from app.models.user import User

SessionDep = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    db: SessionDep,
    session_id: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> User:
    if session_id is None:
        raise NotAuthenticatedError()
    user_id = await get_session(session_id)
    if user_id is None:
        raise NotAuthenticatedError()
    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        raise NotAuthenticatedError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
