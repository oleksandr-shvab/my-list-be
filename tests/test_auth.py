from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.crud.user import authenticate_user

CREDENTIALS = {"email": "player@example.com", "password": "correct horse battery"}


async def test__register__ok(client: AsyncClient) -> None:
    response = await client.post("/auth/register", json=CREDENTIALS)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == CREDENTIALS["email"]
    assert body["is_active"] is True
    assert "password" not in body and "hashed_password" not in body
    assert settings.SESSION_COOKIE_NAME in response.cookies


async def test__login__ok(client: AsyncClient) -> None:
    await client.post("/auth/register", json=CREDENTIALS)

    response = await client.post("/auth/login", json=CREDENTIALS)

    assert response.status_code == 200
    assert response.json()["email"] == CREDENTIALS["email"]
    assert settings.SESSION_COOKIE_NAME in response.cookies


async def test__logout__ok(client: AsyncClient) -> None:
    await client.post("/auth/register", json=CREDENTIALS)

    response = await client.post("/auth/logout")

    assert response.status_code == 204
    assert (await client.get("/auth/me")).status_code == 401


async def test__me__ok(client: AsyncClient) -> None:
    await client.post("/auth/register", json=CREDENTIALS)

    response = await client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["email"] == CREDENTIALS["email"]


async def test__hash_password__long_cyrillic_password_verifies() -> None:
    # 37 Cyrillic letters are 74 bytes: bcrypt rejected anything over 72 bytes with a 500.
    password = "я" * 37

    hashed = await hash_password(password)

    assert await verify_password(password, hashed)
    assert not await verify_password(password + "x", hashed)


async def test__authenticate_user__unknown_email_returns_none(db_session: AsyncSession) -> None:
    assert await authenticate_user(db_session, "nobody@example.com", "whatever password") is None
