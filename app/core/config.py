from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET_KEYS = {"change-me", "changeme", "secret", "password"}
MIN_SECRET_KEY_LENGTH = 32
DEFAULT_PORTS = {"http": 80, "https": 443}


def serialize_origin(value: str, *, strict: bool = False) -> str | None:
    """The origin (`scheme://host[:port]`) of an absolute http(s) URL, written one way: scheme and
    host in lower case, the default port dropped. None if `value` is not such a URL or carries
    credentials. Lenient (default): any path, query or fragment is ignored, so a Referer URL gives
    its origin. Strict, for an `Origin` header: a path other than `/`, and any query or fragment
    (even a bare `?` or `#`), make it None."""
    value = value.strip()
    if strict and ("?" in value or "#" in value):
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not host or parts.username is not None:
        return None
    if strict and parts.path not in ("", "/"):
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


@lru_cache
def _allowed_origins(frontend_url: str, extra_urls: tuple[str, ...]) -> frozenset[str]:
    origins = (serialize_origin(url) for url in (frontend_url, *extra_urls))
    return frozenset(origin for origin in origins if origin is not None)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    ENVIRONMENT: Literal["local", "test", "staging", "production"] = "local"

    DATABASE_URL: str
    # Used only by the test suite (tests/conftest.py); never set in staging or production.
    TEST_DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5433/daily_game_challenge_test"
    )
    REDIS_URL: str = "redis://localhost:6379/0"
    # Used only by the test suite. A different database from REDIS_URL, so a test run never
    # touches the sessions of a running dev server.
    TEST_REDIS_URL: str = "redis://localhost:6379/1"

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT_SECONDS: int = 10
    DB_STATEMENT_TIMEOUT_MS: int = 30_000

    SECRET_KEY: SecretStr

    SESSION_COOKIE_NAME: str = "session_id"
    SESSION_EXPIRE_MINUTES: int = 60 * 24 * 14

    FRONTEND_URL: str = "http://localhost:5173"
    # The API's own origin. Allowed to send unsafe requests only in `local` and `test`, so the
    # docs page's "Try it out" works.
    API_BASE_URL: str = "http://localhost:8000"

    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    @property
    def is_deployed(self) -> bool:
        return self.ENVIRONMENT in ("staging", "production")

    @property
    def is_cookie_secure(self) -> bool:
        return self.ENVIRONMENT not in ("local", "test")

    @property
    def allowed_origins(self) -> frozenset[str]:
        """The origins that may send unsafe requests (see app/core/csrf.py). Cached on the inputs,
        so changing `ENVIRONMENT` or a URL is picked up."""
        extra_urls = () if self.is_deployed else (*self.BACKEND_CORS_ORIGINS, self.API_BASE_URL)
        return _allowed_origins(self.FRONTEND_URL, extra_urls)

    @model_validator(mode="after")
    def refuse_unsafe_config(self) -> Self:
        problems: list[str] = []
        if serialize_origin(self.FRONTEND_URL) is None:
            problems.append("FRONTEND_URL must be an absolute http(s) URL")
        if self.is_deployed:
            secret_key = self.SECRET_KEY.get_secret_value()
            if (
                secret_key.lower() in PLACEHOLDER_SECRET_KEYS
                or len(secret_key) < MIN_SECRET_KEY_LENGTH
            ):
                problems.append(
                    f"SECRET_KEY must not be a placeholder and must be at least "
                    f"{MIN_SECRET_KEY_LENGTH} characters"
                )
            if not self.FRONTEND_URL.startswith("https://"):
                problems.append("FRONTEND_URL must use https")
            for origin in self.BACKEND_CORS_ORIGINS:
                if origin == "*" or "localhost" in origin or "127.0.0.1" in origin:
                    problems.append(f"BACKEND_CORS_ORIGINS must not contain {origin!r}")
        else:
            # These two feed `allowed_origins` only outside staging and production.
            if serialize_origin(self.API_BASE_URL) is None:
                problems.append("API_BASE_URL must be an absolute http(s) URL")
            for origin in self.BACKEND_CORS_ORIGINS:
                if serialize_origin(origin) is None:
                    problems.append(
                        f"BACKEND_CORS_ORIGINS entry {origin!r} must be an absolute http(s) URL"
                    )
        if problems:
            raise ValueError(
                f"Unsafe configuration for ENVIRONMENT={self.ENVIRONMENT}: " + "; ".join(problems)
            )
        return self


settings = Settings()
