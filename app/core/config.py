import ipaddress
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRET_KEYS = {"change-me", "changeme", "secret", "password"}
MIN_SECRET_KEY_LENGTH = 32
# A relative MEDIA_LOCAL_DIR is read from here, so the seed and the server share one folder
# whatever their working directory is.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PORTS = {"http": 80, "https": 443}
# Long enough to pass the length check, so the deployed check refuses it by name.
DEV_MEDIA_SIGNING_KEY = "dev-only-media-signing-key-do-not-use-in-production"


def is_weak_secret(value: str) -> bool:
    return value.lower() in PLACEHOLDER_SECRET_KEYS or len(value) < MIN_SECRET_KEY_LENGTH


# A path prefix made only of plain segments: no `.` or `..`, no empty segment, no escapes.
CLEAN_PATH = re.compile(r"(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)*/?", re.ASCII)


# One label of a host name: letters, digits and inner hyphens, at most 63 characters.
HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
# `urlsplit` silently drops a tab or a newline inside a URL, so refuse anything but printable
# ASCII.
PLAIN_URL_TEXT = re.compile(r"[\x21-\x7e]+", re.ASCII)


def _is_valid_host(host: str) -> bool:
    """`urlsplit` strips the brackets of an IPv6 host."""
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    labels = host.split(".")
    return len(host) <= 253 and all(HOST_LABEL.fullmatch(label) for label in labels)


def is_clean_base_url(value: str) -> bool:
    """Links are built by adding `/v1/media/...`, so allow only a plain path prefix
    (`https://site/api`): no credentials, query, fragment, whitespace, `..` or escapes."""
    if PLAIN_URL_TEXT.fullmatch(value) is None or "?" in value or "#" in value:
        return False
    if serialize_origin(value) is None:
        return False
    parts = urlsplit(value)
    if parts.hostname is None or not _is_valid_host(parts.hostname):
        return False
    return CLEAN_PATH.fullmatch(parts.path) is not None


def is_https(value: str) -> bool:
    """Case-insensitive: `HTTPS://` is the same scheme."""
    return urlsplit(value).scheme.lower() == "https"


def serialize_origin(value: str, *, strict: bool = False) -> str | None:
    """Lower-case scheme and host, default port dropped. Lenient (default) ignores path, query and
    fragment, so a Referer URL gives its origin. Strict, for an `Origin` header, returns None for a
    path other than `/` and for any query or fragment (even a bare `?` or `#`)."""
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
    # Tests only; never set in staging or production.
    TEST_DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5433/daily_game_challenge_test"
    )
    REDIS_URL: str = "redis://localhost:6379/0"
    # A different database from REDIS_URL, so a test run never touches a dev server's sessions.
    TEST_REDIS_URL: str = "redis://localhost:6379/1"

    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT_SECONDS: int = 10
    DB_STATEMENT_TIMEOUT_MS: int = 30_000

    SECRET_KEY: SecretStr

    SESSION_COOKIE_NAME: str = "session_id"
    SESSION_EXPIRE_MINUTES: int = 60 * 24 * 14

    FRONTEND_URL: str = "http://localhost:5173"
    # Allowed to send unsafe requests only in `local` and `test`, so the docs page's "Try it out"
    # works.
    API_BASE_URL: str = "http://localhost:8000"

    BACKEND_CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    MEDIA_STORAGE: Literal["local", "s3"] = "local"
    MEDIA_LOCAL_DIR: str = "var/media"
    # The dev default goes through the frontend's proxy, so a link loads with no CORS and no
    # cookie. With local storage in production: `https://<site>/api`.
    MEDIA_LOCAL_BASE_URL: str = "http://localhost:5173/api"
    # Separate from SECRET_KEY, so a leak of one does not give the other.
    MEDIA_SIGNING_KEY: SecretStr = SecretStr(DEV_MEDIA_SIGNING_KEY)
    # Video links live longer (AC-70, AC-70b): a browser keeps asking for parts of a looping clip.
    MEDIA_IMAGE_LINK_TTL_SECONDS: int = Field(default=600, ge=60, le=3600)
    MEDIA_VIDEO_LINK_TTL_SECONDS: int = Field(default=3600, ge=600, le=86400)
    S3_ENDPOINT_URL: str | None = None
    S3_REGION: str | None = None
    S3_BUCKET: str | None = None
    S3_ACCESS_KEY_ID: str | None = None
    S3_SECRET_ACCESS_KEY: SecretStr | None = None

    @property
    def media_local_path(self) -> Path:
        """A relative MEDIA_LOCAL_DIR is read from the repo root, not the working directory."""
        return PROJECT_ROOT / self.MEDIA_LOCAL_DIR

    @property
    def is_deployed(self) -> bool:
        return self.ENVIRONMENT in ("staging", "production")

    @property
    def is_cookie_secure(self) -> bool:
        return self.ENVIRONMENT not in ("local", "test")

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Cached on its inputs, so a changed `ENVIRONMENT` or URL is picked up."""
        extra_urls = () if self.is_deployed else (*self.BACKEND_CORS_ORIGINS, self.API_BASE_URL)
        return _allowed_origins(self.FRONTEND_URL, extra_urls)

    @model_validator(mode="after")
    def refuse_unsafe_config(self) -> Self:
        problems: list[str] = []
        if serialize_origin(self.FRONTEND_URL) is None:
            problems.append("FRONTEND_URL must be an absolute http(s) URL")
        if self.is_deployed:
            if is_weak_secret(self.SECRET_KEY.get_secret_value()):
                problems.append(
                    f"SECRET_KEY must not be a placeholder and must be at least "
                    f"{MIN_SECRET_KEY_LENGTH} characters"
                )
            if not is_https(self.FRONTEND_URL):
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
        problems.extend(self._media_problems())
        if problems:
            raise ValueError(
                f"Unsafe configuration for ENVIRONMENT={self.ENVIRONMENT}: " + "; ".join(problems)
            )
        return self

    def _media_problems(self) -> list[str]:
        problems: list[str] = []
        if self.MEDIA_STORAGE == "s3":
            s3_values = {
                "S3_ENDPOINT_URL": self.S3_ENDPOINT_URL,
                "S3_REGION": self.S3_REGION,
                "S3_BUCKET": self.S3_BUCKET,
                "S3_ACCESS_KEY_ID": self.S3_ACCESS_KEY_ID,
                "S3_SECRET_ACCESS_KEY": self.S3_SECRET_ACCESS_KEY,
            }
            missing = [name for name, value in s3_values.items() if not value]
            if missing:
                problems.append(f"MEDIA_STORAGE=s3 needs {', '.join(missing)}")
            else:
                endpoint = self.S3_ENDPOINT_URL or ""
                if not is_clean_base_url(endpoint):
                    problems.append(
                        "S3_ENDPOINT_URL must be an absolute http(s) URL with no query, "
                        "fragment or credentials"
                    )
                elif self.is_deployed and not is_https(endpoint):
                    problems.append("S3_ENDPOINT_URL must use https")
            return problems
        if not is_clean_base_url(self.MEDIA_LOCAL_BASE_URL):
            problems.append(
                "MEDIA_LOCAL_BASE_URL must be an absolute http(s) URL with at most a plain path "
                "prefix (no query, fragment or credentials)"
            )
        if self.is_deployed:
            # Local storage can be real in production (the fallback of AC-72c), so its links
            # need a real key.
            signing_key = self.MEDIA_SIGNING_KEY.get_secret_value()
            if signing_key == DEV_MEDIA_SIGNING_KEY or is_weak_secret(signing_key):
                problems.append(
                    f"MEDIA_SIGNING_KEY must not be a placeholder and must be at least "
                    f"{MIN_SECRET_KEY_LENGTH} characters"
                )
            if signing_key == self.SECRET_KEY.get_secret_value():
                problems.append("MEDIA_SIGNING_KEY must differ from SECRET_KEY")
            if not is_https(self.MEDIA_LOCAL_BASE_URL):
                problems.append("MEDIA_LOCAL_BASE_URL must use https")
        return problems


settings = Settings()
