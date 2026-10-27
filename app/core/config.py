from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET_KEYS = {"change-me", "changeme", "secret", "password"}
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    ENVIRONMENT: Literal["local", "test", "staging", "production"] = "local"

    DATABASE_URL: str
    # Used only by the test suite (tests/conftest.py); never set in staging or production.
    TEST_DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/item_list_test"
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

    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    @property
    def is_deployed(self) -> bool:
        return self.ENVIRONMENT in ("staging", "production")

    @property
    def is_cookie_secure(self) -> bool:
        return self.ENVIRONMENT not in ("local", "test")

    @model_validator(mode="after")
    def refuse_unsafe_deployed_config(self) -> Self:
        if not self.is_deployed:
            return self
        problems: list[str] = []
        secret_key = self.SECRET_KEY.get_secret_value()
        if secret_key.lower() in PLACEHOLDER_SECRET_KEYS or len(secret_key) < MIN_SECRET_KEY_LENGTH:
            problems.append(
                f"SECRET_KEY must not be a placeholder and must be at least "
                f"{MIN_SECRET_KEY_LENGTH} characters"
            )
        if not self.FRONTEND_URL.startswith("https://"):
            problems.append("FRONTEND_URL must use https")
        for origin in self.BACKEND_CORS_ORIGINS:
            if origin == "*" or "localhost" in origin or "127.0.0.1" in origin:
                problems.append(f"BACKEND_CORS_ORIGINS must not contain {origin!r}")
        if problems:
            raise ValueError(
                f"Unsafe configuration for ENVIRONMENT={self.ENVIRONMENT}: " + "; ".join(problems)
            )
        return self


settings = Settings()
