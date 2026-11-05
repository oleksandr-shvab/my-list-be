from typing import Annotated

from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.exceptions import NotAuthenticatedError
from app.crud.session import get_session
from app.crud.user import get_user_by_id
from app.media.storage import MediaStorage, get_media_storage
from app.models.user import User

SessionDep = Annotated[AsyncSession, Depends(get_db)]
# For a route that streams a file: `scope="function"` closes the session (and so ends its
# transaction and returns its connection to the pool) when the route function returns, before the
# body is sent. With the default scope it would stay open until the last byte was sent.
FunctionScopedSessionDep = Annotated[AsyncSession, Depends(get_db, scope="function")]
# Makes the signed media links of the puzzle view and tells the media route where the files are.
MediaStorageDep = Annotated[MediaStorage, Depends(get_media_storage)]


async def get_optional_user(
    db: SessionDep,
    session_id: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> User | None:
    """The signed-in user, or None for a guest. A missing, unknown or expired cookie and an
    inactive user all mean a guest, never 401 (AC-42, AC-81)."""
    if session_id is None:
        return None
    user_id = await get_session(session_id)
    if user_id is None:
        return None
    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        return None
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


async def get_current_user(user: OptionalUser) -> User:
    if user is None:
        raise NotAuthenticatedError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
