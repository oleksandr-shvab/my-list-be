import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from app.core import health
from app.main import app

PROBLEM_JSON = "application/problem+json"


async def test__login_with_wrong_password__problem_details_and_request_id(
    client: AsyncClient,
) -> None:
    await client.post("/auth/register", json={"email": "a@example.com", "password": "password-one"})

    response = await client.post(
        "/auth/login",
        json={"email": "a@example.com", "password": "wrong-password"},
        headers={"X-Request-ID": "trace-123"},
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    body = response.json()
    assert body["code"] == "invalid_credentials"
    assert body["status"] == 401
    assert body["request_id"] == "trace-123" == response.headers["x-request-id"]


async def test__validation_error__codes_without_echoing_input(client: AsyncClient) -> None:
    secret = "xQ9!z"

    response = await client.post(
        "/auth/register", json={"email": "a@example.com", "password": secret}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [
        {"field": "password", "code": "too_short", "params": {"min": 8}},
    ]
    assert secret not in response.text


async def test__unhandled_exception__json_500_with_request_id() -> None:
    probe = APIRouter()

    @probe.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    app.include_router(probe)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/boom")
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/boom"]

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["request_id"] == response.headers["x-request-id"]
    assert "secret internal detail" not in response.text


async def test__healthz__ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test__readyz__ok(client: AsyncClient) -> None:
    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test__readyz__dependency_down__503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def redis_down() -> bool:
        return False

    monkeypatch.setattr(health, "check_redis", redis_down)

    response = await client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "service_unavailable"
    assert body["params"] == {"failed": ["redis"]}
