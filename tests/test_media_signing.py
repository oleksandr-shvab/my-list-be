"""Signed links (AC-70, AC-70b, AC-73) and the two storages. No database, no network."""

import datetime as dt
import inspect
import os
import socket
import subprocess
import sys
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest

from app.core.config import PROJECT_ROOT, settings
from app.media import signing
from app.media.signing import is_media_key, is_valid_link, sign_link
from app.media.storage import (
    LocalMediaStorage,
    S3MediaStorage,
    build_media_storage,
    get_media_storage,
)
from tests.factories import (
    IMAGE_TTL_SECONDS,
    TEST_SIGNING_KEY,
    VIDEO_TTL_SECONDS,
    make_local_storage,
    media_key,
)

KEY = media_key(1, 1, "source")
SECRET = b"s" * 32
EXPIRES = 2_000_000_000
NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)


def _is_valid(
    *,
    key: str = KEY,
    expires: int = EXPIRES,
    signature: str | None = None,
    secret: bytes = SECRET,
    now: int = EXPIRES - 1,
) -> bool:
    signature = signature if signature is not None else sign_link(KEY, EXPIRES, SECRET)
    return is_valid_link(key, expires, signature, secret=secret, now=now)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, True, id="valid"),
        pytest.param({"now": 0}, True, id="long_before_the_expiry"),
        # Expiry (AC-73): the link works until the expiry second, and not at it.
        pytest.param({"now": EXPIRES}, False, id="expired_at_the_expiry_second"),
        pytest.param({"now": EXPIRES + 1}, False, id="expired_after"),
        # A signature covers the key, the expiry and the secret, and nothing else.
        pytest.param({"key": media_key(1, 2, "source")}, False, id="other_key"),
        pytest.param({"expires": EXPIRES + 3600}, False, id="extended_expiry"),
        pytest.param({"secret": b"t" * 32}, False, id="other_secret"),
        pytest.param({"signature": ""}, False, id="empty_signature"),
        pytest.param({"signature": "A" * 43}, False, id="wrong_signature"),
        pytest.param({"signature": sign_link(KEY, EXPIRES, SECRET) + "A"}, False, id="longer"),
        pytest.param({"signature": sign_link(KEY, EXPIRES, SECRET)[:-1]}, False, id="shorter"),
        pytest.param({"signature": "é" * 43}, False, id="non_ascii_signature"),
    ],
)
def test__is_valid_link__signature_and_expiry(kwargs: dict[str, object], expected: bool) -> None:
    assert _is_valid(**kwargs) is expected  # type: ignore[arg-type]


def test__is_valid_link__an_expired_link_with_the_right_signature_is_refused() -> None:
    # The signature alone is right; only the expiry rule can refuse it.
    signature = sign_link(KEY, EXPIRES, SECRET)

    assert is_valid_link(KEY, EXPIRES, signature, secret=SECRET, now=EXPIRES + 10) is False


def test__is_valid_link__compares_the_signature_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare = signing.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(signing.hmac, "compare_digest", spy)

    assert _is_valid() is True
    assert _is_valid(signature="A" * 43) is False

    assert len(calls) == 2
    assert all(isinstance(left, bytes) and isinstance(right, bytes) for left, right in calls)
    assert calls[0][0] == calls[0][1]


def test__sign_link__is_unpadded_base64url_of_a_sha256_mac() -> None:
    signature = sign_link(KEY, EXPIRES, SECRET)

    assert len(signature) == 43
    assert set(signature) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert signature == sign_link(KEY, EXPIRES, SECRET)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (KEY, True),
        ("0" * 64, True),
        ("a" * 63, False),
        ("a" * 65, False),
        ("A" * 64, False),  # Upper case is not a hex digest we write.
        ("g" * 64, False),
        ("", False),
        ("..", False),
        ("../" + "a" * 61, False),
        (".." + "a" * 62, False),
        ("a" * 63 + "/", False),
        ("a" * 63 + "\n", False),  # `$` would accept a trailing newline; `fullmatch` does not.
        ("a" * 32 + "%2e" * 10 + "a" * 2, False),
        ("٣" * 64, False),  # Arabic-Indic digits.
    ],
)
def test__is_media_key__only_a_lowercase_sha256_hex_digest(value: str, expected: bool) -> None:
    assert is_media_key(value) is expected


# Link lifetimes (AC-70, AC-70b).


@pytest.mark.parametrize(
    ("content_type", "ttl"),
    [
        ("image/webp", IMAGE_TTL_SECONDS),  # Images and posters: the short lifetime.
        ("video/mp4", VIDEO_TTL_SECONDS),
        ("video/webm", VIDEO_TTL_SECONDS),
    ],
)
def test__local_signed_url__lifetime_follows_the_content_type_and_the_link_verifies(
    content_type: str, ttl: int
) -> None:
    storage = make_local_storage()

    url, expires_at = storage.signed_url(KEY, content_type, NOW)

    assert expires_at == NOW + dt.timedelta(seconds=ttl)
    parts = urlsplit(url)
    assert (
        f"{parts.scheme}://{parts.netloc}{parts.path}" == f"https://media.test/api/v1/media/{KEY}"
    )
    query = parse_qs(parts.query)
    assert query["exp"] == [str(int(expires_at.timestamp()))]
    assert is_valid_link(
        KEY,
        int(query["exp"][0]),
        query["sig"][0],
        secret=TEST_SIGNING_KEY,
        now=int(NOW.timestamp()),
    )
    # One second after the expiry, the same link is refused.
    assert not is_valid_link(
        KEY,
        int(query["exp"][0]),
        query["sig"][0],
        secret=TEST_SIGNING_KEY,
        now=int(expires_at.timestamp()),
    )


def test__local_signed_url__expiry_is_whole_seconds() -> None:
    now = NOW + dt.timedelta(microseconds=999_999)

    _, expires_at = make_local_storage().signed_url(KEY, "image/webp", now)

    assert expires_at == NOW + dt.timedelta(seconds=IMAGE_TTL_SECONDS)


# The S3 storage: presigning is local arithmetic, so it needs no network.


def test__s3_signed_url__video_link_has_the_bucket_host_and_the_video_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("presigning must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    storage = S3MediaStorage(
        endpoint_url="https://fsn1.your-objectstorage.com",
        region="fsn1",
        bucket="clue-media",
        access_key_id="AKIAFAKEFAKEFAKE",
        secret_access_key="fake-secret",
        image_ttl_seconds=IMAGE_TTL_SECONDS,
        video_ttl_seconds=VIDEO_TTL_SECONDS,
    )

    video_url, video_expiry = storage.signed_url(KEY, "video/mp4", NOW)
    image_url, image_expiry = storage.signed_url(KEY, "image/webp", NOW)

    video = urlsplit(video_url)
    assert video.netloc == "clue-media.fsn1.your-objectstorage.com"
    assert video.path == f"/{KEY}"
    video_query = parse_qs(video.query)
    assert video_query["X-Amz-Expires"] == [str(VIDEO_TTL_SECONDS)]
    assert video_query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    # A browser must not keep the file past the link: `private`, no `max-age`, and an `Expires`
    # that is the link's own expiry.
    assert video_query["response-cache-control"] == ["private"]
    assert video_query["response-expires"] == [format_datetime(video_expiry, usegmt=True)]
    assert video_expiry == NOW + dt.timedelta(seconds=VIDEO_TTL_SECONDS)
    image_query = parse_qs(urlsplit(image_url).query)
    assert image_query["X-Amz-Expires"] == [str(IMAGE_TTL_SECONDS)]
    assert image_query["response-expires"] == [format_datetime(image_expiry, usegmt=True)]
    assert "max-age" not in image_url
    assert image_expiry == NOW + dt.timedelta(seconds=IMAGE_TTL_SECONDS)


# Building the storage once.


def test__s3_storage__uses_its_own_boto3_session_not_the_shared_default_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)

    S3MediaStorage(
        endpoint_url="https://fsn1.your-objectstorage.com",
        region="fsn1",
        bucket="clue-media",
        access_key_id="id",
        secret_access_key="secret",
        image_ttl_seconds=IMAGE_TTL_SECONDS,
        video_ttl_seconds=VIDEO_TTL_SECONDS,
    )

    assert boto3.DEFAULT_SESSION is None


async def test__get_media_storage__is_async_and_returns_the_one_built_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "MEDIA_STORAGE", "local")
    monkeypatch.setattr(settings, "MEDIA_LOCAL_DIR", "var/media")
    build_media_storage.cache_clear()
    try:
        first = await get_media_storage()
        second = await get_media_storage()
    finally:
        build_media_storage.cache_clear()

    assert inspect.iscoroutinefunction(get_media_storage)  # Not run in the threadpool.
    assert first is second
    assert isinstance(first, LocalMediaStorage)
    # A relative folder is read from the repo root, not from the working directory.
    assert first.directory == PROJECT_ROOT / "var" / "media"


def test__app_import__does_not_load_boto3_when_media_is_stored_locally() -> None:
    # A fresh interpreter: this test process has imported boto3 already.
    code = "import sys; import app.main; print('boto3' in sys.modules, 'botocore' in sys.modules)"

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env={**os.environ, "MEDIA_STORAGE": "local"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False"
