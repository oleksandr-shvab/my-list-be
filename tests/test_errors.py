import datetime as dt
import decimal

import pytest
from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from app.core import health
from app.core.errors import _validation_error_items, field_from_location
from app.main import app

PROBLEM_JSON = "application/problem+json"


async def test__login_with_wrong_password__problem_details_and_request_id(
    client: AsyncClient,
) -> None:
    await client.post(
        "/v1/auth/register", json={"email": "a@example.com", "password": "password-one"}
    )

    response = await client.post(
        "/v1/auth/login",
        json={"email": "a@example.com", "password": "wrong-password"},
        headers={"X-Request-ID": "trace-123"},
    )

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.headers["x-content-type-options"] == "nosniff"  # On every problem response.
    body = response.json()
    assert body["code"] == "invalid_credentials"
    assert body["status"] == 401
    assert body["request_id"] == "trace-123" == response.headers["x-request-id"]


async def test__validation_error__codes_without_echoing_input(client: AsyncClient) -> None:
    secret = "xQ9!z"

    response = await client.post(
        "/v1/auth/register", json={"email": "a@example.com", "password": secret}
    )

    assert response.status_code == 422
    assert response.headers["x-content-type-options"] == "nosniff"
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

    routes_before = list(app.router.routes)
    app.include_router(probe)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.get("/boom")
    finally:
        # include_router adds a nested router object, so restore the whole list.
        app.router.routes[:] = routes_before
        app.openapi_schema = None

    assert response.status_code == 500
    assert response.headers["x-content-type-options"] == "nosniff"  # The global 500 handler too.
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


@pytest.mark.parametrize(
    ("location", "field"),
    [
        (("body", "email"), "email"),
        (("path", "date"), "date"),
        (("query", "limit"), "limit"),
        (("body", "entries", 3, "date"), "entries.3.date"),
        # Only the first part names where the value came from; a later part with that name stays.
        (("body", "query"), "query"),
        (("body", "filters", "path"), "filters.path"),
        (("header", "x-request-id"), "x-request-id"),
        (("cookie", "session_id"), "session_id"),
        (("body", "cookie"), "cookie"),
        (("query", "body"), "body"),
        (("filters", "query"), "filters.query"),
        ((), ""),
    ],
)
def test__field_from_location__drops_only_the_leading_source(
    location: tuple[str | int, ...], field: str
) -> None:
    assert field_from_location(location) == field


# Pydantic error types and their context, as `RequestValidationError.errors()` gives them, and the
# stable item the frontend gets. A type with no entry keeps pydantic's own name.
@pytest.mark.parametrize(
    ("error", "item"),
    [
        (
            {"type": "greater_than_equal", "loc": ("query", "limit"), "ctx": {"ge": 1}},
            {"field": "limit", "code": "too_small", "params": {"min": 1}},
        ),
        (
            {"type": "less_than_equal", "loc": ("query", "limit"), "ctx": {"le": 100}},
            {"field": "limit", "code": "too_large", "params": {"max": 100}},
        ),
        (
            {"type": "int_parsing", "loc": ("query", "limit")},
            {"field": "limit", "code": "invalid_type", "params": {}},
        ),
        (
            {"type": "int_parsing_size", "loc": ("query", "limit")},
            {"field": "limit", "code": "invalid_type", "params": {}},
        ),
        # A bound that is not JSON (`ge=date(...)`) is encoded, not left to break the response.
        (
            {
                "type": "greater_than_equal",
                "loc": ("query", "since"),
                "ctx": {"ge": dt.date(2026, 1, 1)},
            },
            {"field": "since", "code": "too_small", "params": {"min": "2026-01-01"}},
        ),
        (
            {
                "type": "less_than_equal",
                "loc": ("query", "price"),
                "ctx": {"le": decimal.Decimal("9.5")},
            },
            {"field": "price", "code": "too_large", "params": {"max": 9.5}},
        ),
        # A bound JSON cannot hold (`inf`, `nan`) is left out, so the response still renders.
        (
            {"type": "less_than_equal", "loc": ("query", "price"), "ctx": {"le": float("inf")}},
            {"field": "price", "code": "too_large", "params": {}},
        ),
        (
            {"type": "greater_than_equal", "loc": ("query", "price"), "ctx": {"ge": float("nan")}},
            {"field": "price", "code": "too_small", "params": {}},
        ),
        (
            {
                "type": "less_than_equal",
                "loc": ("query", "price"),
                "ctx": {"le": decimal.Decimal("Infinity")},
            },
            {"field": "price", "code": "too_large", "params": {}},
        ),
        (
            {"type": "string_too_short", "loc": ("body", "password"), "ctx": {"min_length": 8}},
            {"field": "password", "code": "too_short", "params": {"min": 8}},
        ),
        (
            {"type": "too_long", "loc": ("body", "previous"), "ctx": {"max_length": 20}},
            {"field": "previous", "code": "too_long", "params": {"max": 20}},
        ),
        (
            {"type": "something_new", "loc": ("body", "x")},
            {"field": "x", "code": "something_new", "params": {}},
        ),
    ],
)
def test__validation_error_items__maps_pydantic_types_to_stable_codes(
    error: dict[str, object], item: dict[str, object]
) -> None:
    assert _validation_error_items(RequestValidationError([error])) == [item]
