import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.core.config import settings
from app.core.exceptions import IncorrectPasswordError
from app.crud.session import create_session, get_session
from app.crud.user import authenticate_user, create_user


async def test__update_password__ok(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="update@example.com", password="OldPass123")
    token = await create_session(user.id)

    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    response = await client.post(
        "/auth/update-password",
        json={
            "old_password": "OldPass123",
            "new_password": "NewPass123",
            "confirm_password": "NewPass123",
        },
    )

    assert response.status_code == 204
    assert await authenticate_user(db_session, user.email, "NewPass123") is not None


async def test__update_password__revokes_other_sessions(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="revoke@example.com", password="OldPass123")
    current_token = await create_session(user.id)
    other_token = await create_session(user.id)

    await service.update_password(db_session, user, current_token, "OldPass123", "NewPass123")

    assert await get_session(current_token) == user.id
    assert await get_session(other_token) is None


async def test__update_password__wrong_old_password_raises(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="wrongold@example.com", password="OldPass123")
    token = await create_session(user.id)

    with pytest.raises(IncorrectPasswordError):
        await service.update_password(db_session, user, token, "WrongPass123", "NewPass123")

    assert await authenticate_user(db_session, user.email, "OldPass123") is not None
