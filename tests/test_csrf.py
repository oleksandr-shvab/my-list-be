from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, serialize_origin, settings
from app.main import app

LOGIN_PATH = "/v1/auth/login"
LOGOUT_PATH = "/v1/auth/logout"
CREDENTIALS = {"email": "nobody@example.com", "password": "correct-horse-battery"}
# The rows below that name other origins (port 5174, https, a suffix host) are foreign as long as
# FRONTEND_URL is the default.
ALLOWED = serialize_origin(settings.FRONTEND_URL) or ""
API_ORIGIN = serialize_origin(settings.API_BASE_URL) or ""
assert ALLOWED
assert API_ORIGIN


@pytest.fixture
async def bare_client(client: AsyncClient) -> AsyncGenerator[AsyncClient, None]:
    """Like `client` (it reuses its `get_db` override) but sends no `Origin` of its own."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as bare:
        yield bare


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({"Origin": ALLOWED}, 401),
        ({"Origin": "https://evil.example"}, 403),
        ({}, 403),
        ({"Referer": f"{ALLOWED}/play?x=1"}, 401),
        ({"Referer": "https://evil.example/play"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://localhost:5174"}, 403),
        ({"Origin": "https://localhost:5173"}, 403),
        ({"Origin": f"{ALLOWED}.evil.example"}, 403),
        ({"Origin": f"{ALLOWED}/path"}, 403),
        ({"Origin": f"{ALLOWED}?"}, 403),
        ({"Origin": f"{ALLOWED}#"}, 403),
        ({"Origin": f"{ALLOWED}/"}, 401),
        ({"Origin": "http://evil.example", "Referer": f"{ALLOWED}/"}, 403),
        ({"Origin": ALLOWED.upper()}, 401),
        ({"Origin": API_ORIGIN}, 401),
    ],
    ids=[
        "allowed_origin",
        "foreign_origin",
        "no_headers",
        "allowed_referer_only",
        "foreign_referer_only",
        "null_origin",
        "other_port",
        "other_scheme",
        "suffix_host",
        "origin_with_path",
        "origin_with_bare_query",
        "origin_with_bare_fragment",
        "origin_with_trailing_slash",
        "foreign_origin_beats_allowed_referer",
        "upper_case_origin",
        "api_own_origin_in_test",
    ],
)
async def test__login_origin__table(
    bare_client: AsyncClient, headers: dict[str, str], expected_status: int
) -> None:
    response = await bare_client.post(LOGIN_PATH, json=CREDENTIALS, headers=headers)

    assert response.status_code == expected_status
    if expected_status == 403:
        assert response.json()["code"] == "csrf_origin_mismatch"


async def test__api_own_origin__refused_in_production(
    bare_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    own = await bare_client.post(LOGIN_PATH, json=CREDENTIALS, headers={"Origin": API_ORIGIN})
    frontend = await bare_client.post(LOGIN_PATH, json=CREDENTIALS, headers={"Origin": ALLOWED})

    assert own.status_code == 403
    assert frontend.status_code == 401


async def test__valid_origin_with_two_referers__passes(bare_client: AsyncClient) -> None:
    response = await bare_client.post(
        LOGIN_PATH,
        json=CREDENTIALS,
        headers=[("Origin", ALLOWED), ("Referer", f"{ALLOWED}/a"), ("Referer", f"{ALLOWED}/b")],  # type: ignore[arg-type]
    )

    assert response.status_code == 401


async def test__no_origin_and_two_referers__403(bare_client: AsyncClient) -> None:
    response = await bare_client.post(
        LOGIN_PATH,
        json=CREDENTIALS,
        headers=[("Referer", f"{ALLOWED}/a"), ("Referer", f"{ALLOWED}/b")],  # type: ignore[arg-type]
    )

    assert response.status_code == 403


async def test__duplicate_origin_header__403(bare_client: AsyncClient) -> None:
    response = await bare_client.post(
        LOGIN_PATH,
        json=CREDENTIALS,
        headers=[("Origin", ALLOWED), ("Origin", ALLOWED)],  # type: ignore[arg-type]
    )

    assert response.status_code == 403


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "PURGE"])
@pytest.mark.parametrize(
    ("origin", "expected_status"),
    [(ALLOWED, 405), ("https://evil.example", 403), (None, 403)],
    ids=["allowed", "foreign", "none"],
)
async def test__other_unsafe_methods__origin_checked_before_routing(
    bare_client: AsyncClient, method: str, origin: str | None, expected_status: int
) -> None:
    headers = {"Origin": origin} if origin else {}

    response = await bare_client.request(method, LOGIN_PATH, headers=headers)

    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("origin", "expected_status"),
    [(ALLOWED, 204), ("https://evil.example", 403), (None, 403)],
    ids=["allowed", "foreign", "none"],
)
async def test__logout_without_body__origin_checked_not_content_type(
    bare_client: AsyncClient, origin: str | None, expected_status: int
) -> None:
    headers = {"Origin": origin} if origin else {}

    response = await bare_client.post(LOGOUT_PATH, headers=headers)

    assert response.status_code == expected_status


async def test__bad_origin_with_bad_body_and_no_session__403_not_422_or_401(
    bare_client: AsyncClient,
) -> None:
    response = await bare_client.post(
        LOGOUT_PATH,
        content=b"not json",
        headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
    )
    login = await bare_client.post(
        LOGIN_PATH, json={"email": 1}, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert login.status_code == 403
    assert login.json()["code"] == "csrf_origin_mismatch"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
async def test__non_json_body__415(client: AsyncClient, method: str) -> None:
    response = await client.request(
        method, LOGIN_PATH, content="email=a", headers={"Content-Type": "text/plain"}
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


@pytest.mark.parametrize(
    "length_headers",
    [{"Content-Length": "0", "Transfer-Encoding": "chunked"}, {"Content-Length": "-1"}],
    ids=["transfer_encoding_beats_zero_length", "negative_length"],
)
async def test__body_hidden_by_length_header__415(
    client: AsyncClient, length_headers: dict[str, str]
) -> None:
    response = await client.post(
        LOGIN_PATH, content=b"x", headers={"Content-Type": "text/plain", **length_headers}
    )

    assert response.status_code == 415


async def test__body_without_content_type__415(client: AsyncClient) -> None:
    response = await client.post(LOGIN_PATH, content=b"{}")

    assert response.status_code == 415


async def test__json_with_charset_and_upper_case__passes(client: AsyncClient) -> None:
    response = await client.post(
        LOGIN_PATH,
        content=b'{"email": "nobody@example.com", "password": "correct-horse-battery"}',
        headers={"Content-Type": "Application/JSON; charset=utf-8"},
    )

    assert response.status_code == 401


async def test__safe_methods_with_foreign_origin__not_checked(bare_client: AsyncClient) -> None:
    response = await bare_client.get("/healthz", headers={"Origin": "https://evil.example"})
    options = await bare_client.options(LOGIN_PATH, headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    assert options.status_code != 403


async def test__csrf_errors__have_no_store_and_request_id(bare_client: AsyncClient) -> None:
    forbidden = await bare_client.post(LOGIN_PATH, json=CREDENTIALS)
    unsupported = await bare_client.post(
        LOGIN_PATH, content=b"x", headers={"Origin": ALLOWED, "Content-Type": "text/plain"}
    )

    for response in (forbidden, unsupported):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:5173", "http://localhost:5173"),
        ("HTTP://LocalHost:5173/", "http://localhost:5173"),
        ("http://localhost:80", "http://localhost"),
        ("http://localhost:5173/a?b=c#d", "http://localhost:5173"),
        ("https://example.com:443/a/b?c=d", "https://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://[::1]:5173", "http://[::1]:5173"),
        ("null", None),
        ("*", None),
        ("", None),
        ("ftp://example.com", None),
        ("http://", None),
        ("http://user@example.com", None),
        ("http://example.com:notaport", None),
    ],
)
def test__serialize_origin__one_form(value: str, expected: str | None) -> None:
    assert serialize_origin(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:5173", "http://localhost:5173"),
        ("http://localhost:5173/", "http://localhost:5173"),
        ("http://localhost:5173/path", None),
        ("http://localhost:5173?", None),
        ("http://localhost:5173#", None),
        ("http://localhost:5173/?a=b", None),
    ],
)
def test__serialize_origin_strict__refuses_path_query_fragment(
    value: str, expected: str | None
) -> None:
    assert serialize_origin(value, strict=True) == expected


BASE_SETTINGS = {
    "DATABASE_URL": "postgresql+asyncpg://x",
    "SECRET_KEY": "x" * 40,
    "FRONTEND_URL": "https://app.example.com",
    "BACKEND_CORS_ORIGINS": [],
    "MEDIA_SIGNING_KEY": "m" * 40,
    "MEDIA_LOCAL_BASE_URL": "https://app.example.com/api",
}


@pytest.mark.parametrize(
    ("environment", "frontend_url"),
    [
        ("local", ""),
        ("local", "localhost:5173"),
        ("local", "ftp://example.com"),
        ("local", "*"),
        ("test", "http://"),
        ("staging", "https://"),
        ("staging", "https://user@app.example.com"),
        ("production", "https://"),
    ],
)
def test__settings__refuse_frontend_url_that_is_not_an_origin(
    environment: str, frontend_url: str
) -> None:
    with pytest.raises(ValueError, match="FRONTEND_URL must be an absolute http"):
        Settings(
            _env_file=None,
            **{**BASE_SETTINGS, "ENVIRONMENT": environment, "FRONTEND_URL": frontend_url},
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"API_BASE_URL": "localhost:8000"}, "API_BASE_URL must be an absolute http"),
        ({"API_BASE_URL": ""}, "API_BASE_URL must be an absolute http"),
        ({"BACKEND_CORS_ORIGINS": ["*"]}, "BACKEND_CORS_ORIGINS entry '\\*'"),
        ({"BACKEND_CORS_ORIGINS": ["http://localhost:5173", "nope"]}, "entry 'nope'"),
    ],
)
@pytest.mark.parametrize("environment", ["local", "test"])
def test__settings__refuse_unusable_origin_settings_where_used(
    environment: str, override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **{**BASE_SETTINGS, "ENVIRONMENT": environment, **override})


def test__settings__origin_settings_not_checked_where_unused() -> None:
    # In production API_BASE_URL and BACKEND_CORS_ORIGINS never reach `allowed_origins`.
    deployed = Settings(
        _env_file=None, **{**BASE_SETTINGS, "ENVIRONMENT": "production", "API_BASE_URL": "nope"}
    )

    assert deployed.allowed_origins == {"https://app.example.com"}
