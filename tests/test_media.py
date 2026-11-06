"""The local media route (AC-72, AC-72b, AC-73): `Range` parsing, the link checks in the service,
and the route's answers. The files live in `tmp_path`; nothing reaches the network."""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine
from app.core.exceptions import InvalidMediaLinkError, PuzzleDataError, PuzzleNotFoundError
from app.main import app
from app.media import service as media_service
from app.media.ranges import RangeKind, classify_range
from app.media.service import MediaFile, open_media
from app.media.signing import sign_link
from app.media.storage import S3MediaStorage, get_media_storage
from app.models.game import Game
from app.models.puzzle import Puzzle, PuzzleStatus
from tests.conftest import SetNow
from tests.factories import (
    TEST_SIGNING_KEY,
    create_game,
    create_puzzle,
    create_titles,
    make_local_storage,
    media_key,
)

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=3)
FILE_SIZE = 1000
# The same bytes in every file, so a slice of the body names the offsets it came from.
FILE_BYTES = bytes(index % 251 for index in range(FILE_SIZE))


# Range parsing, with no route (AC-72b, RFC 9110 section 14).


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        pytest.param(None, RangeKind.FULL, id="no_header"),
        pytest.param("bytes=0-1", RangeKind.SINGLE, id="first_two_bytes"),
        pytest.param("bytes=0-0", RangeKind.SINGLE, id="first_byte"),
        pytest.param("bytes=100-", RangeKind.SINGLE, id="open_ended"),
        pytest.param("bytes=-500", RangeKind.SINGLE, id="suffix"),
        pytest.param("bytes=-5000", RangeKind.SINGLE, id="suffix_longer_than_the_file"),
        pytest.param("bytes=999-", RangeKind.SINGLE, id="last_byte"),
        pytest.param("bytes=0-99999", RangeKind.SINGLE, id="end_past_the_file_is_clamped"),
        pytest.param("bytes=990-5000", RangeKind.SINGLE, id="start_inside_end_outside"),
        pytest.param("BYTES=0-1", RangeKind.SINGLE, id="unit_is_case_insensitive"),
        pytest.param("bytes= 0-1 ", RangeKind.SINGLE, id="spaces_around"),
        # A unit we do not know is ignored (RFC 9110 section 14.2), whatever follows the `=`.
        pytest.param("items=0-1", RangeKind.FULL, id="other_unit"),
        pytest.param("pages=abc", RangeKind.FULL, id="other_unit_with_nonsense"),
        # Several ranges: the whole file.
        pytest.param("bytes=0-1,5-6", RangeKind.FULL, id="two_ranges"),
        pytest.param("bytes=0-1,5000-6000", RangeKind.FULL, id="two_ranges_one_outside"),
        # Outside the file.
        pytest.param("bytes=1000-", RangeKind.NOT_SATISFIABLE, id="starts_at_the_size"),
        pytest.param("bytes=5000-6000", RangeKind.NOT_SATISFIABLE, id="beyond_the_end"),
        pytest.param("bytes=-0", RangeKind.NOT_SATISFIABLE, id="empty_suffix"),
        pytest.param("bytes=2000-3000,4000-", RangeKind.NOT_SATISFIABLE, id="all_outside"),
        # Malformed.
        pytest.param("", RangeKind.NOT_SATISFIABLE, id="empty"),
        pytest.param("bytes", RangeKind.NOT_SATISFIABLE, id="no_equals_sign"),
        pytest.param("bytes=", RangeKind.NOT_SATISFIABLE, id="no_ranges"),
        pytest.param("bytes=-", RangeKind.NOT_SATISFIABLE, id="only_a_dash"),
        pytest.param("bytes=abc", RangeKind.NOT_SATISFIABLE, id="not_numbers"),
        pytest.param("bytes=a-b", RangeKind.NOT_SATISFIABLE, id="letters"),
        pytest.param("bytes=5-2", RangeKind.NOT_SATISFIABLE, id="end_before_start"),
        pytest.param("bytes=0-1,x", RangeKind.NOT_SATISFIABLE, id="one_bad_of_two"),
        pytest.param("bytes=0-1,", RangeKind.SINGLE, id="trailing_comma"),
        pytest.param("bytes=,0-1", RangeKind.SINGLE, id="leading_comma"),
        pytest.param("bytes=0-1,,5-6", RangeKind.FULL, id="empty_element_between_two_ranges"),
        pytest.param("bytes=,", RangeKind.NOT_SATISFIABLE, id="only_commas"),
        pytest.param("bytes=, ,", RangeKind.NOT_SATISFIABLE, id="only_commas_and_spaces"),
        pytest.param("bytes=+1-2", RangeKind.NOT_SATISFIABLE, id="plus_sign"),
        pytest.param("bytes=1_0-20", RangeKind.NOT_SATISFIABLE, id="underscore"),
        pytest.param("bytes=٣-٥", RangeKind.NOT_SATISFIABLE, id="other_script_digits"),
        pytest.param("bytes=" + "9" * 5000 + "-", RangeKind.NOT_SATISFIABLE, id="huge_number"),
        pytest.param("0-1", RangeKind.NOT_SATISFIABLE, id="no_unit"),
        pytest.param("=0-1", RangeKind.NOT_SATISFIABLE, id="empty_unit"),
        pytest.param("by tes=0-1", RangeKind.NOT_SATISFIABLE, id="unit_that_is_not_a_token"),
    ],
)
def test__classify_range__table(header: str | None, expected: RangeKind) -> None:
    assert classify_range(header, FILE_SIZE) == expected


def test__classify_range__an_empty_file_has_no_satisfiable_range() -> None:
    assert classify_range("bytes=0-", 0) == RangeKind.NOT_SATISFIABLE
    assert classify_range("bytes=-5", 0) == RangeKind.NOT_SATISFIABLE


# The link checks and the lookup, at the service level (AC-12, AC-73, R-19).


@dataclass
class MediaWorld:
    """A visible puzzle with its files on disk, and the clock fixed at `NOW`."""

    db: AsyncSession
    directory: Path
    puzzle: Puzzle
    image_key: str
    video_key: str
    webm_key: str


@pytest.fixture
async def world(db_session: AsyncSession, set_now: SetNow, tmp_path: Path) -> MediaWorld:
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    (answer,) = await create_titles(db_session, "Half-Life 2")
    puzzle = await create_puzzle(
        db_session, game=game, answer=answer, puzzle_date=TODAY, with_webm=True
    )
    keys = [media_key(puzzle.id, step, name) for step, name in [(1, "source"), (6, "source")]]
    webm_key = media_key(puzzle.id, 6, "webm")
    for key in (*keys, webm_key):
        (tmp_path / key).write_bytes(FILE_BYTES)
    return MediaWorld(db_session, tmp_path, puzzle, keys[0], keys[1], webm_key)


def _link(key: str, *, expires: int | None = None, signature: str | None = None) -> dict[str, str]:
    expires = expires if expires is not None else int(NOW.timestamp()) + 600
    signature = signature if signature is not None else sign_link(key, expires, TEST_SIGNING_KEY)
    return {"expires": str(expires), "signature": signature}


async def _open(world: MediaWorld, key: str, **link: str):
    return await open_media(
        world.db,
        storage=make_local_storage(world.directory),
        key=key,
        now=NOW,
        **(link or _link(key)),
    )


async def test__open_media__a_valid_link_gives_the_stored_content_type_and_the_remaining_time(
    world: MediaWorld,
) -> None:
    media = await _open(world, world.video_key)

    assert media.path == world.directory / world.video_key
    assert media.content_type == "video/mp4"
    assert media.remaining_seconds == 600
    assert media.stat_result.st_size == FILE_SIZE
    assert (await _open(world, world.webm_key)).content_type == "video/webm"
    assert (await _open(world, world.image_key)).content_type == "image/webp"


@pytest.mark.parametrize(
    "bad_key",
    [
        "..",
        "../" + "a" * 61,
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "",
        "a" * 63 + "\n",
        "%2e%2e" + "a" * 58,
    ],
)
async def test__open_media__a_key_of_the_wrong_shape_is_refused_even_with_a_valid_signature(
    world: MediaWorld, bad_key: str
) -> None:
    # The test signs the bad key itself, so only the key check can refuse it.
    with pytest.raises(InvalidMediaLinkError):
        await _open(world, bad_key)


@pytest.mark.parametrize(
    "link",
    [
        pytest.param({"signature": "A" * 43}, id="wrong_signature"),
        pytest.param({"signature": ""}, id="no_signature"),
        pytest.param({"expires": "0"}, id="expired_long_ago_with_a_signature_for_another_time"),
        pytest.param({"expires": "abc"}, id="expiry_not_a_number"),
        pytest.param({"expires": ""}, id="no_expiry"),
        pytest.param({"expires": "-5"}, id="negative_expiry"),
        pytest.param({"expires": "1e12"}, id="expiry_in_float_form"),
        pytest.param({"expires": "٣" * 10}, id="expiry_in_other_script_digits"),
        pytest.param({"expires": "9" * 5000}, id="expiry_with_5000_digits"),
    ],
)
async def test__open_media__a_bad_signature_or_expiry_is_refused(
    world: MediaWorld, link: dict[str, str]
) -> None:
    good = _link(world.image_key)

    with pytest.raises(InvalidMediaLinkError):
        await _open(world, world.image_key, **{**good, **link})


@pytest.mark.parametrize(
    ("seconds_left", "is_valid"),
    [(600, True), (1, True), (0, False), (-1, False), (-3600, False)],
)
async def test__open_media__expiry_is_checked_against_the_clock(
    world: MediaWorld, seconds_left: int, is_valid: bool
) -> None:
    link = _link(world.image_key, expires=int(NOW.timestamp()) + seconds_left)

    if is_valid:
        assert (await _open(world, world.image_key, **link)).remaining_seconds == seconds_left
    else:
        with pytest.raises(InvalidMediaLinkError):
            await _open(world, world.image_key, **link)


@pytest.mark.parametrize(
    ("change", "game_change"),
    [
        pytest.param({"status": PuzzleStatus.DRAFT}, {}, id="unscheduled_to_draft"),
        pytest.param({"status": PuzzleStatus.APPROVED}, {}, id="unscheduled_to_approved"),
        pytest.param({"date": TODAY + dt.timedelta(days=1)}, {}, id="date_in_the_future"),
        pytest.param({}, {"launch_date": TODAY + dt.timedelta(days=1)}, id="before_the_launch"),
        pytest.param({}, {"is_enabled": False}, id="game_disabled"),
    ],
)
async def test__open_media__a_file_of_a_puzzle_that_is_not_visible_is_puzzle_not_found(
    world: MediaWorld, change: dict[str, object], game_change: dict[str, object]
) -> None:
    link = _link(world.image_key)
    assert (await _open(world, world.image_key, **link)).content_type == "image/webp"
    if change:
        await world.db.execute(update(Puzzle).where(Puzzle.id == world.puzzle.id).values(**change))
    if game_change:
        await world.db.execute(
            update(Game).where(Game.id == world.puzzle.game_id).values(**game_change)
        )

    with pytest.raises(PuzzleNotFoundError):
        await _open(world, world.image_key, **link)


async def test__open_media__a_signed_key_that_is_in_no_puzzle_is_puzzle_not_found(
    world: MediaWorld,
) -> None:
    unknown = media_key(0, 0, "nothing")

    with pytest.raises(PuzzleNotFoundError):
        await _open(world, unknown)


async def test__open_media__a_row_with_no_file_is_a_server_fault(world: MediaWorld) -> None:
    (world.directory / world.image_key).unlink()

    with pytest.raises(PuzzleDataError):
        await _open(world, world.image_key)


# The route (AC-72, AC-72b, AC-73). One happy path, then the table of ranges and refusals.


@pytest.fixture
async def route(client: AsyncClient, world: MediaWorld) -> MediaWorld:
    """`world`, with the app's storage pointed at its folder."""
    storage = make_local_storage(world.directory)
    app.dependency_overrides[get_media_storage] = lambda: storage
    return world


def _path(key: str, *, expires: int | None = None, signature: str | None = None) -> str:
    link = _link(key, expires=expires, signature=signature)
    return f"/v1/media/{key}?exp={link['expires']}&sig={link['signature']}"


async def test__get_media__serves_the_file_with_its_stored_type_and_safe_headers(
    client: AsyncClient, route: MediaWorld
) -> None:
    response = await client.get(_path(route.image_key))

    assert response.status_code == 200
    assert response.content == FILE_BYTES
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    # The cache lifetime is the time the link has left, never longer (AC-77).
    assert response.headers["cache-control"] == "private, max-age=600"
    # The media route is not part of the OpenAPI document (R-17).
    assert "/v1/media/{key}" not in (await client.get("/openapi.json")).json()["paths"]


EXPIRED = int(NOW.timestamp()) - 1
EXPIRES_SOON = int(NOW.timestamp()) + 100
ROUTE_ROWS = [
    # method, which file, Range header, link changes, status, Content-Range, body
    pytest.param("GET", "video", None, {}, 200, None, FILE_BYTES, id="full_get"),
    pytest.param("HEAD", "video", None, {}, 200, None, b"", id="head"),
    pytest.param("GET", "video", "bytes=0-1", {}, 206, "bytes 0-1/1000", FILE_BYTES[0:2], id="0-1"),
    pytest.param(
        "GET", "video", "bytes=100-", {}, 206, "bytes 100-999/1000", FILE_BYTES[100:], id="100-"
    ),
    pytest.param(
        "GET", "video", "bytes=-500", {}, 206, "bytes 500-999/1000", FILE_BYTES[500:], id="-500"
    ),
    pytest.param(
        "GET", "video", "bytes=0-99999", {}, 206, "bytes 0-999/1000", FILE_BYTES, id="clamped_end"
    ),
    pytest.param(
        "GET", "video", "bytes=999-", {}, 206, "bytes 999-999/1000", FILE_BYTES[999:], id="999-"
    ),
    pytest.param("HEAD", "video", "bytes=0-1", {}, 206, "bytes 0-1/1000", b"", id="head_0-1"),
    pytest.param("GET", "webm", "bytes=2-3", {}, 206, "bytes 2-3/1000", FILE_BYTES[2:4], id="webm"),
    # Several ranges: the whole file, not multipart.
    pytest.param("GET", "video", "bytes=0-1,5-6", {}, 200, None, FILE_BYTES, id="two_ranges"),
    # Outside the file or malformed: 416 with `Content-Range: bytes */<size>` (AC-72b).
    pytest.param("GET", "video", "bytes=1000-", {}, 416, "bytes */1000", None, id="unsatisfiable"),
    pytest.param("GET", "video", "bytes=5-2", {}, 416, "bytes */1000", None, id="reversed"),
    pytest.param("GET", "video", "bytes=abc", {}, 416, "bytes */1000", None, id="malformed"),
    # A unit we do not know is ignored: the whole file, not Starlette's plain-text 400.
    pytest.param("GET", "video", "items=0-1", {}, 200, None, FILE_BYTES, id="other_unit"),
    pytest.param(
        "GET", "video", "bytes=0-1,", {}, 206, "bytes 0-1/1000", FILE_BYTES[0:2], id="0-1,"
    ),
    pytest.param(
        "GET", "video", "bytes=,0-1", {}, 206, "bytes 0-1/1000", FILE_BYTES[0:2], id=",0-1"
    ),
    pytest.param("HEAD", "video", "bytes=9999-", {}, 416, "bytes */1000", None, id="head_416"),
    # The signature and the expiry count on every request, ranged or not (AC-72b, AC-73).
    pytest.param(
        "GET", "video", "bytes=0-1", {"expires": EXPIRED}, 403, None, None, id="expired_with_range"
    ),
    pytest.param("GET", "video", None, {"expires": EXPIRED}, 403, None, None, id="expired"),
    # The cache lifetime is what the link has left, not a lifetime of the storage (AC-77).
    pytest.param(
        "GET", "video", None, {"expires": EXPIRES_SOON}, 200, None, FILE_BYTES, id="max_age_is_left"
    ),
    pytest.param(
        "GET", "video", "bytes=0-1", {"signature": "A" * 43}, 403, None, None, id="bad_signature"
    ),
    pytest.param(
        "GET", "video", "bytes=0-1", {"signature": ""}, 403, None, None, id="no_signature"
    ),
    # A 416 does not leak that the range was the problem when the link is also bad.
    pytest.param(
        "GET", "video", "bytes=9999-", {"signature": "A" * 43}, 403, None, None, id="bad_and_416"
    ),
    pytest.param("GET", "video", None, {"unschedule": True}, 404, None, None, id="unscheduled"),
    pytest.param(
        "GET", "video", "bytes=9999-", {"unschedule": True}, 404, None, None, id="unscheduled_416"
    ),
]


@pytest.mark.parametrize(
    ("method", "which", "range_header", "changes", "status", "content_range", "body"), ROUTE_ROWS
)
async def test__media_route__ranges_and_refusals(
    client: AsyncClient,
    route: MediaWorld,
    method: str,
    which: str,
    range_header: str | None,
    changes: dict[str, object],
    status: int,
    content_range: str | None,
    body: bytes | None,
) -> None:
    key = {"video": route.video_key, "webm": route.webm_key}[which]
    expires = changes.get("expires")
    signature = changes.get("signature")
    assert expires is None or isinstance(expires, int)
    assert signature is None or isinstance(signature, str)
    if changes.get("unschedule"):
        await route.db.execute(
            update(Puzzle).where(Puzzle.id == route.puzzle.id).values(status=PuzzleStatus.DRAFT)
        )
    headers = {} if range_header is None else {"Range": range_header}

    response = await client.request(
        method, _path(key, expires=expires, signature=signature), headers=headers
    )

    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers.get("content-range") == content_range
    if status in (200, 206):
        assert (
            response.headers["content-type"] == {"video": "video/mp4", "webm": "video/webm"}[which]
        )
        assert response.headers["accept-ranges"] == "bytes"
        max_age = 600 if expires is None else expires - int(NOW.timestamp())
        assert response.headers["cache-control"] == f"private, max-age={max_age}"
        assert response.content == body
        if method == "HEAD":
            expected_length = {"bytes=0-1": 2}.get(range_header or "", FILE_SIZE)
            assert response.headers["content-length"] == str(expected_length)
    else:
        codes = {403: "invalid_media_link", 404: "puzzle_not_found", 416: "range_not_satisfiable"}
        if method != "HEAD":  # A HEAD answer has no body to read.
            assert response.json()["code"] == codes[status]
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(("range_header", "status"), [(None, 200), ("bytes=0-1", 206)])
async def test__media_route__a_head_answer_has_no_body_on_the_wire(
    route: MediaWorld, range_header: str | None, status: int
) -> None:
    # httpx's ASGI transport drops the body of a HEAD answer, so only a raw call can see it.
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    link = _link(route.video_key)
    headers = [(b"host", b"test")]
    if range_header is not None:
        headers.append((b"range", range_header.encode()))
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "HEAD",
            "scheme": "http",
            "path": f"/v1/media/{route.video_key}",
            "raw_path": b"",
            "query_string": f"exp={link['expires']}&sig={link['signature']}".encode(),
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        },
        receive,
        send,
    )

    assert sent[0]["status"] == status
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")  # type: ignore[call-overload]
    assert body == b""


async def test__media_route__s3_storage_serves_nothing_locally(
    client: AsyncClient, route: MediaWorld
) -> None:
    s3 = S3MediaStorage(
        endpoint_url="https://fsn1.your-objectstorage.com",
        region="fsn1",
        bucket="clue-media",
        access_key_id="id",
        secret_access_key="secret",
        image_ttl_seconds=600,
        video_ttl_seconds=3600,
    )
    app.dependency_overrides[get_media_storage] = lambda: s3

    response = await client.get(_path(route.image_key))

    assert response.status_code == 403
    assert response.json()["code"] == "invalid_media_link"
    assert response.headers["x-content-type-options"] == "nosniff"


# The database connection is given back before the body streams (a streamed clip can take a long
# time, and a pooled connection must not sit idle in a transaction meanwhile).


async def test__media_route__no_connection_is_checked_out_while_the_body_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    big_file = tmp_path / "big.bin"
    big_file.write_bytes(b"x" * 300_000)  # Five chunks of 64 KB.
    before = engine.pool.checkedout()
    checked_out_in_lookup: list[int] = []

    async def lookup_that_uses_the_database(db: AsyncSession, **_: object) -> MediaFile:
        await db.execute(text("SELECT 1"))  # Checks a real connection out of the pool.
        checked_out_in_lookup.append(engine.pool.checkedout())
        return MediaFile(
            path=big_file,
            content_type="image/webp",
            stat_result=big_file.stat(),
            remaining_seconds=600,
        )

    monkeypatch.setattr(media_service, "open_media", lookup_that_uses_the_database)
    # The real `get_db` stays in place on purpose: this checks the app's own pool.
    app.dependency_overrides[get_media_storage] = lambda: make_local_storage(tmp_path)
    checked_out_per_chunk: list[int] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            checked_out_per_chunk.append(engine.pool.checkedout())

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/v1/media/{media_key(0, 0, 'x')}",
        "raw_path": b"",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    try:
        await app(scope, receive, send)
    finally:
        app.dependency_overrides.pop(get_media_storage, None)

    assert checked_out_in_lookup == [before + 1]  # The lookup did hold a connection.
    assert len(checked_out_per_chunk) >= 5
    assert set(checked_out_per_chunk) == {before}  # And it was gone before the first chunk.
