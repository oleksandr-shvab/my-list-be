import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service
from app.auth.schemas import ResetPasswordRequest
from app.core.exceptions import InvalidOrExpiredResetTokenError
from app.core.redis import redis_client
from app.crud.password_reset import (
    PASSWORD_RESET_USER_KEY_PREFIX,
    create_password_reset_token,
    get_password_reset_user_id,
)
from app.crud.user import authenticate_user, create_user

GENERIC_FORGOT_PASSWORD_DETAIL = "If an account with that email exists, a reset link has been sent."


async def test__forgot_password__ok(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="existing@example.com", password="OldPass123")

    response = await client.post("/auth/forgot-password", json={"email": user.email})

    assert response.status_code == 202
    assert response.json() == {"detail": GENERIC_FORGOT_PASSWORD_DETAIL}
    token = await redis_client.get(f"{PASSWORD_RESET_USER_KEY_PREFIX}{user.id}")
    assert token is not None


async def test__reset_password__ok(client: AsyncClient, db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="reset@example.com", password="OldPass123")
    token = await create_password_reset_token(user.id)

    response = await client.post(
        "/auth/reset-password",
        json={"token": token, "new_password": "NewPass123", "confirm_password": "NewPass123"},
    )

    assert response.status_code == 204
    assert await authenticate_user(db_session, user.email, "NewPass123") is not None
    assert await authenticate_user(db_session, user.email, "OldPass123") is None


async def test__create_password_reset_token__invalidates_previous(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="repeat@example.com", password="OldPass123")

    first_token = await create_password_reset_token(user.id)
    second_token = await create_password_reset_token(user.id)

    assert await get_password_reset_user_id(first_token) is None
    assert await get_password_reset_user_id(second_token) == user.id


async def test__reset_password__token_is_single_use(db_session: AsyncSession) -> None:
    user = await create_user(db_session, email="singleuse@example.com", password="OldPass123")
    token = await create_password_reset_token(user.id)

    await service.reset_password(db_session, token, "NewPass123")

    with pytest.raises(InvalidOrExpiredResetTokenError):
        await service.reset_password(db_session, token, "AnotherPass123")


def test__reset_password_request__mismatched_passwords_raises() -> None:
    with pytest.raises(ValidationError):
        ResetPasswordRequest(token="x", new_password="NewPass123", confirm_password="Different123")
