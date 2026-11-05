from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    DEV_MEDIA_SIGNING_KEY,
    PROJECT_ROOT,
    Settings,
    is_clean_base_url,
    is_weak_secret,
)

DEPLOYED = {
    "ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql+asyncpg://u:p@db/app",
    "SECRET_KEY": "x" * 40,
    "FRONTEND_URL": "https://example.com",
    "BACKEND_CORS_ORIGINS": [],
    "MEDIA_SIGNING_KEY": "m" * 40,
    "MEDIA_LOCAL_BASE_URL": "https://example.com/api",
}
S3 = {
    "MEDIA_STORAGE": "s3",
    "S3_ENDPOINT_URL": "https://fsn1.your-objectstorage.com",
    "S3_REGION": "fsn1",
    "S3_BUCKET": "bucket",
    "S3_ACCESS_KEY_ID": "id",
    "S3_SECRET_ACCESS_KEY": "secret",
}


@pytest.mark.parametrize(
    "base_url", ["https://example.com", "https://example.com/", "https://example.com/api/"]
)
def test__production_settings__a_clean_media_base_url_boots(base_url: str) -> None:
    assert Settings(**{**DEPLOYED, "MEDIA_LOCAL_BASE_URL": base_url}).is_deployed


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://localhost:5173/api", True),  # The dev default.
        ("https://example.com", True),
        ("https://example.com/", True),
        ("https://example.com/api", True),
        ("https://example.com/api/v1-media_2/", True),
        ("https://example.com?x=1", False),
        ("https://example.com/api?", False),
        ("https://example.com/api#", False),
        ("https://example.com/api#top", False),
        ("https://user:pw@example.com/api", False),
        ("https://example.com/..", False),
        ("https://example.com/../api", False),
        ("https://example.com/./api", False),
        ("https://example.com//api", False),
        ("https://example.com/a b", False),
        ("https://example.com/api/%2e%2e", False),
        ("https://example.com/api;x=1", False),
        (" https://example.com/api", False),
        ("https://example.com/api\n", False),
        ("ftp://example.com/api", False),
        ("example.com/api", False),
        ("", False),
        # The host: a name, an IPv4 address or a bracketed IPv6 address, and nothing else.
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000/api", True),
        ("http://my-host.example.com:8443/api", True),
        ("http://exa mple.com/api", False),
        ("http://ex<a>.com/api", False),
        ("http://%65x.com/api", False),
        ("http://exa\tmple.com/api", False),  # urlsplit would drop the tab and accept it.
        ("http://example.com\n/api", False),
        ("http://-example.com/api", False),
        ("http://example-.com/api", False),
        ("http://example..com/api", False),
        ("http://exa_mple.com/api", False),
        ("http://é.com/api", False),
        ("http://" + "a" * 64 + ".com/api", False),
        ("http://[::1/api", False),
        ("http://[zzz]/api", False),
        ("http://example.com:99999/api", False),
        ("http://example.com:80a/api", False),
        ("http:///api", False),
        ("http://:80/api", False),
    ],
)
def test__is_clean_base_url__table(base_url: str, expected: bool) -> None:
    assert is_clean_base_url(base_url) is expected


@pytest.mark.parametrize("base_url", ["http://localhost:5173/api?x=1", "http://localhost:5173/a#b"])
@pytest.mark.parametrize("environment", ["local", "test"])
def test__local_settings__an_unclean_media_base_url_refuses_to_boot(
    environment: str, base_url: str
) -> None:
    with pytest.raises(ValidationError, match="MEDIA_LOCAL_BASE_URL"):
        Settings(
            **{**DEPLOYED, "ENVIRONMENT": environment, "MEDIA_LOCAL_BASE_URL": base_url}  # type: ignore[arg-type]
        )


def test__local_settings__an_http_s3_endpoint_is_fine_outside_deployed_environments() -> None:
    # A local MinIO has no TLS.
    local = {**DEPLOYED, **S3, "ENVIRONMENT": "local", "S3_ENDPOINT_URL": "http://localhost:9000"}
    assert Settings(**local).MEDIA_STORAGE == "s3"  # type: ignore[arg-type]


@pytest.mark.parametrize("weak", ["change-me", "CHANGEME", "secret", "short", "x" * 31])
def test__is_weak_secret__placeholders_and_short_values(weak: str) -> None:
    assert is_weak_secret(weak) is True
    assert is_weak_secret("k" * 32) is False


def test__media_local_path__a_relative_dir_is_read_from_the_repo_root_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Settings(**{**DEPLOYED, "ENVIRONMENT": "local", "MEDIA_LOCAL_DIR": "var/media"})  # type: ignore[arg-type]
    absolute = Settings(**{**DEPLOYED, "ENVIRONMENT": "local", "MEDIA_LOCAL_DIR": str(tmp_path)})  # type: ignore[arg-type]

    assert relative.media_local_path == PROJECT_ROOT / "var" / "media"
    assert (PROJECT_ROOT / "pyproject.toml").is_file()  # The root is the backend repo's.
    assert absolute.media_local_path == tmp_path


@pytest.mark.parametrize(
    "override",
    [
        # The scheme is compared without regard to letter case (urls are case-insensitive there).
        {"FRONTEND_URL": "HTTPS://example.com"},
        {"MEDIA_LOCAL_BASE_URL": "HTTPS://example.com/api"},
        {**S3, "S3_ENDPOINT_URL": "HTTPS://fsn1.your-objectstorage.com"},
        {**S3, "S3_ENDPOINT_URL": "https://fsn1.your-objectstorage.com/"},
    ],
)
def test__production_settings__an_upper_case_https_scheme_is_https(
    override: dict[str, object],
) -> None:
    assert Settings(**{**DEPLOYED, **override}).is_deployed  # type: ignore[arg-type]


@pytest.mark.parametrize("environment", ["local", "production"])
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fsn1.your-objectstorage.com?x=1",
        "https://fsn1.your-objectstorage.com#top",
        "https://key:secret@fsn1.your-objectstorage.com",
        "https://:secret@fsn1.your-objectstorage.com",
        "ftp://fsn1.your-objectstorage.com",
        "fsn1.your-objectstorage.com",
        "https://fsn1 .your-objectstorage.com",
        "https://",
    ],
)
def test__s3_settings__an_endpoint_that_is_not_a_clean_http_url_refuses_to_boot(
    environment: str, endpoint: str
) -> None:
    with pytest.raises(ValidationError, match="S3_ENDPOINT_URL"):
        Settings(**{**DEPLOYED, **S3, "ENVIRONMENT": environment, "S3_ENDPOINT_URL": endpoint})  # type: ignore[arg-type]


def test__production_settings__safe_values_boot() -> None:
    assert Settings(**DEPLOYED).is_deployed
    # The bounds are inclusive, and S3 needs no signing key of its own.
    bounds = {"MEDIA_IMAGE_LINK_TTL_SECONDS": 60, "MEDIA_VIDEO_LINK_TTL_SECONDS": 86400}
    assert Settings(**DEPLOYED, **bounds).is_deployed
    s3 = {**DEPLOYED, **S3, "MEDIA_SIGNING_KEY": "change-me"}
    assert Settings(**s3).MEDIA_STORAGE == "s3"


@pytest.mark.parametrize(
    "override",
    [
        {"SECRET_KEY": "change-me"},
        {"SECRET_KEY": "too-short"},
        {"FRONTEND_URL": "http://example.com"},
        {"BACKEND_CORS_ORIGINS": ["http://localhost:5173"]},
        # Local media storage can be real in production, so its links need a real key and https.
        {"MEDIA_SIGNING_KEY": "change-me"},
        {"MEDIA_SIGNING_KEY": "too-short"},
        {"MEDIA_SIGNING_KEY": DEV_MEDIA_SIGNING_KEY},
        {"MEDIA_LOCAL_BASE_URL": "http://example.com/api"},
        # The media key is its own secret: never the same as SECRET_KEY.
        {"MEDIA_SIGNING_KEY": DEPLOYED["SECRET_KEY"]},
        # The base of local links is a clean URL: at most a plain path prefix.
        {"MEDIA_LOCAL_BASE_URL": "https://example.com/api?x=1"},
        {"MEDIA_LOCAL_BASE_URL": "https://example.com/api#top"},
        {"MEDIA_LOCAL_BASE_URL": "https://user:pw@example.com/api"},
        {"MEDIA_LOCAL_BASE_URL": "https://example.com/../api"},
        # S3 links in a deployed environment use https.
        {**S3, "S3_ENDPOINT_URL": "http://fsn1.your-objectstorage.com"},
        # S3 needs every value.
        {**S3, "S3_BUCKET": None},
        {**S3, "S3_SECRET_ACCESS_KEY": None},
        {**S3, "S3_ENDPOINT_URL": None},
        # The link lifetimes are bounded (AC-70, AC-70b).
        {"MEDIA_IMAGE_LINK_TTL_SECONDS": 59},
        {"MEDIA_IMAGE_LINK_TTL_SECONDS": 3601},
        {"MEDIA_VIDEO_LINK_TTL_SECONDS": 599},
        {"MEDIA_VIDEO_LINK_TTL_SECONDS": 86401},
    ],
)
def test__production_settings__unsafe_values_refuse_to_boot(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**{**DEPLOYED, **override})
