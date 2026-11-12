"""The IGDB client over `httpx.MockTransport` and hand-made fixtures (AC-3 to AC-8, AC-85).

No network, no database. The fixtures are in `tests/fixtures/igdb/` (see its README)."""

import asyncio
import datetime as dt
import itertools
import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.content.fetch import (
    ForbiddenHostError,
    ResponseTooLargeError,
    build_client,
    send_capped,
)
from app.content.igdb import (
    DropReason,
    IgdbAuthError,
    IgdbClient,
    IgdbError,
    IgdbUnavailable,
    InvalidWhereError,
    SkipReason,
    parse_game,
)
from app.content.settings import ContentSettings, MissingSettingError

FIXTURES = Path(__file__).parent / "fixtures" / "igdb"
CLIENT_ID = "fixture-client-id"
CLIENT_SECRET = "fixture-client-secret-value"
FIXTURE_TOKEN = "fixture-access-token-0001"
RENEWED_TOKEN = "fixture-access-token-0002"
# Nothing of this may reach a log line, an exception text or a URL.
SECRETS = (CLIENT_SECRET, FIXTURE_TOKEN, RENEWED_TOKEN)
# Fixed, so "next year" is 2027.
NOW = dt.datetime(2026, 11, 12, tzinfo=dt.UTC)
MAX_YEAR = 2027
NOTHING: tuple[dict[str, int], dict[str, int]] = ({}, {})


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def ok(fixture: str) -> httpx.Response:
    return httpx.Response(200, json=load(fixture))


def failure(status: int, fixture: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=load(fixture), headers=headers)


def corrupt_gzip_reply() -> httpx.Response:
    async def body():
        yield b"this is not gzip"

    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=body())


def edge_record(game_id: int, released: dt.datetime) -> dict[str, Any]:
    return {
        "id": game_id,
        "name": f"Edge {game_id}",
        "slug": f"edge-{game_id}",
        "first_release_date": int(released.timestamp()),
    }


def token_reply(token: str = FIXTURE_TOKEN) -> httpx.Response:
    return httpx.Response(200, json={**load("token.json"), "access_token": token})


class Server:
    """Plays the two hosts from scripts. A request that no script answers fails the test, so a
    loop that never ends is a failure and not a hang."""

    def __init__(
        self,
        games: list[httpx.Response | Exception],
        tokens: list[httpx.Response] | None = None,
    ):
        self.games = list(games)
        self.tokens = [token_reply()] if tokens is None else list(tokens)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        replies = {"id.twitch.tv": self.tokens, "api.igdb.com": self.games}.get(request.url.host)
        assert replies, f"unexpected request to {request.url.host}"
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def on(self, host: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.host == host]


async def no_sleep(seconds: float) -> None:
    return None


def make_igdb(handler: Callable[[httpx.Request], Any], **kwargs: Any) -> IgdbClient:
    return IgdbClient(
        CLIENT_ID, SecretStr(CLIENT_SECRET), transport=httpx.MockTransport(handler), **kwargs
    )


@pytest.mark.parametrize(
    (
        "games",
        "tokens",
        "page_size",
        "expected_ids",
        "error",
        "game_calls",
        "token_calls",
        "sleeps",
        "counted",
    ),
    [
        pytest.param(
            [ok("games_page_1.json"), ok("games_page_2.json")],
            None,
            2,
            [1001, 1002, 1003],
            None,
            2,
            1,
            [],
            NOTHING,
            id="two pages are joined in order",
        ),
        pytest.param(
            [failure(401, "error_401.json"), ok("games_page_2.json")],
            [token_reply(), token_reply(RENEWED_TOKEN)],
            2,
            [1003],
            None,
            2,
            2,
            [],
            NOTHING,
            id="a refused token is renewed once and the request repeated",
        ),
        pytest.param(
            [failure(401, "error_401.json"), failure(401, "error_401.json")],
            [token_reply(), token_reply(RENEWED_TOKEN)],
            2,
            [],
            IgdbAuthError,
            2,
            2,
            [],
            NOTHING,
            id="a second 401 stops the run",
        ),
        pytest.param(
            [failure(429, "error_429.json", **{"Retry-After": "3"}), ok("games_page_2.json")],
            None,
            2,
            [1003],
            None,
            2,
            1,
            [3.0],
            NOTHING,
            id="429 waits for Retry-After and then succeeds",
        ),
        pytest.param(
            [failure(503, "error_503.json")] * 6,
            None,
            2,
            [],
            IgdbUnavailable,
            6,
            1,
            [1.0, 2.0, 4.0, 8.0, 16.0],
            NOTHING,
            id="six 503s stop after five retries with growing waits",
        ),
        pytest.param(
            [httpx.ConnectTimeout("timed out"), ok("games_page_2.json")],
            None,
            2,
            [1003],
            None,
            2,
            1,
            [1.0],
            NOTHING,
            id="a transport error is retried and then succeeds",
        ),
        pytest.param(
            [httpx.ConnectTimeout("timed out")] * 6,
            None,
            2,
            [],
            IgdbUnavailable,
            6,
            1,
            [1.0, 2.0, 4.0, 8.0, 16.0],
            NOTHING,
            id="six transport errors stop after five retries",
        ),
        pytest.param(
            [httpx.Response(302, headers={"Location": "https://evil.example/games"})],
            None,
            2,
            [],
            IgdbError,
            1,
            1,
            [],
            NOTHING,
            id="a redirect is not followed",
        ),
        pytest.param(
            [ok("games_invalid.json")],
            None,
            500,
            [2001],
            None,
            1,
            1,
            [],
            (
                {"wrong_shape": 4, "name_without_letters": 1, "year_out_of_range": 3},
                {"screenshot_invalid": 3},
            ),
            id="bad records are skipped and bad screenshots dropped, each counted apart",
        ),
        pytest.param(
            [ok("games_page_2.json")],
            [httpx.Response(503, json=load("error_503.json")), token_reply()],
            2,
            [1003],
            None,
            1,
            2,
            [1.0],
            NOTHING,
            id="a 503 from the token service is retried, not taken for bad credentials",
        ),
        pytest.param(
            [],
            [httpx.Response(400, json={"status": 400, "message": "invalid client secret"})],
            2,
            [],
            IgdbAuthError,
            0,
            1,
            [],
            NOTHING,
            id="a 400 from the token service is a credentials error, not retried",
        ),
        pytest.param(
            [corrupt_gzip_reply(), ok("games_page_2.json")],
            None,
            2,
            [1003],
            None,
            2,
            1,
            [1.0],
            NOTHING,
            id="a corrupt compressed body is retried",
        ),
        pytest.param(
            [
                httpx.Response(
                    200,
                    json=[
                        edge_record(4001, dt.datetime(2027, 12, 31, 23, 59, 59, tzinfo=dt.UTC)),
                        edge_record(4002, dt.datetime(2028, 1, 1, tzinfo=dt.UTC)),
                    ],
                )
            ],
            None,
            500,
            [4001],
            None,
            1,
            1,
            [],
            ({"year_out_of_range": 1}, {}),
            id="next year is accepted and the year after it is skipped",
        ),
    ],
)
async def test__igdb_client__scripted_answers(
    games, tokens, page_size, expected_ids, error, game_calls, token_calls, sleeps, counted, caplog
):
    caplog.set_level(logging.DEBUG)
    server = Server(games, tokens)
    sleeps_seen: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps_seen.append(seconds)

    ids: list[int] = []
    raised: IgdbError | None = None
    async with make_igdb(server, sleep=record_sleep) as igdb:
        try:
            async for game in igdb.games(now=NOW, page_size=page_size):
                ids.append(game.id)
        except IgdbError as caught:
            raised = caught

    assert ids == expected_ids
    assert (dict(igdb.skipped), dict(igdb.dropped)) == counted
    assert type(raised) is (error or type(None))
    assert len(server.on("api.igdb.com")) == game_calls
    assert len(server.on("id.twitch.tv")) == token_calls
    # The spacer sleeps too; only the waits for a retry are listed here.
    assert [seconds for seconds in sleeps_seen if seconds >= 1] == sleeps
    assert {request.url.host for request in server.requests} <= {"api.igdb.com", "id.twitch.tv"}
    # The secret is in the body of the token request, and nowhere else.
    assert CLIENT_SECRET in server.on("id.twitch.tv")[0].content.decode()
    texts = [caplog.text, str(raised), repr(raised), *(str(r.url) for r in server.requests)]
    assert not [text for text in texts for secret in SECRETS if secret in text]


async def test__igdb_client__paging_and_token_in_requests():
    server = Server([ok("games_page_1.json"), ok("games_page_2.json")])
    async with make_igdb(server, sleep=no_sleep) as igdb:
        pages = [page async for page in igdb.iter_pages(where="game_type = 0", page_size=2)]

    assert [len(page) for page in pages] == [2, 1]
    first, second = (request.content.decode() for request in server.on("api.igdb.com"))
    assert "where id > 0 & (game_type = 0)" in first and "limit 2;" in first
    assert "sort id asc" in first
    assert "where id > 1002 & (game_type = 0)" in second
    request = server.on("api.igdb.com")[0]
    assert request.headers["Authorization"] == f"Bearer {FIXTURE_TOKEN}"
    assert request.headers["Client-ID"] == CLIENT_ID

    # The same paging with no condition of the caller's.
    plain = Server([ok("games_page_1.json"), ok("games_page_2.json")])
    async with make_igdb(plain, sleep=no_sleep) as igdb:
        [page async for page in igdb.iter_pages(page_size=2)]
    first, second = (request.content.decode() for request in plain.on("api.igdb.com"))
    assert "where id > 0;" in first
    assert "where id > 1002;" in second

    # A condition that would change the paging is refused before any request.
    refused = Server([])
    async with make_igdb(refused, sleep=no_sleep) as igdb:
        for where in [
            "a = 1; b = 2",
            "a = 1 & sort id desc",
            "a = 1 & limit 1",
            "OFFSET 5",
            "fields *",
        ]:
            with pytest.raises(InvalidWhereError):
                [page async for page in igdb.iter_pages(where=where)]
    assert refused.requests == []


async def test__igdb_client__keeps_the_limits():
    now = 0.0

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    starts: list[float] = []
    queries: list[str] = []
    open_now = 0
    most_open = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal open_now, most_open
        if request.url.host == "id.twitch.tv":
            return token_reply()
        starts.append(now)
        queries.append(request.content.decode())
        open_now += 1
        most_open = max(most_open, open_now)
        await asyncio.sleep(0.01)
        open_now -= 1
        return httpx.Response(200, json=[])

    async def read_one_page(igdb: IgdbClient) -> None:
        async for _ in igdb.iter_pages():
            pass

    async with make_igdb(handler, clock=lambda: now, sleep=fake_sleep) as igdb:
        await asyncio.gather(*(read_one_page(igdb) for _ in range(20)))

    # 20 starts, none sooner than 250 ms after the previous one: at most 4 in any second.
    assert len(starts) == 20
    # With no page size given, a page is 500 records, IGDB's maximum.
    assert all("limit 500;" in query for query in queries)
    assert all(later - earlier >= 0.25 for earlier, later in itertools.pairwise(starts))
    # 20 callers wait on a slow mock: the peak is the limit, not the number of callers.
    assert most_open == 8

    # A request that waited for its slot while another renewed the token uses the new token.
    renewed = asyncio.Event()
    tokens_sent: list[str] = []
    token_requests: list[httpx.Request] = []

    async def wait_for_the_renewal(seconds: float) -> None:
        await renewed.wait()

    async def renewing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            # The first token request opens the run; the second is the renewal.
            token_requests.append(request)
            if len(token_requests) == 1:
                return token_reply()
            renewed.set()
            return token_reply(RENEWED_TOKEN)
        sent = request.headers["Authorization"].removeprefix("Bearer ")
        tokens_sent.append(sent)
        if sent == FIXTURE_TOKEN:
            await asyncio.sleep(0)  # lets the second caller queue behind this request
            return failure(401, "error_401.json")
        return httpx.Response(200, json=[])

    async def read_page(igdb: IgdbClient) -> None:
        async for _ in igdb.iter_pages():
            pass

    async with make_igdb(renewing_handler, sleep=wait_for_the_renewal) as igdb:
        await asyncio.gather(read_page(igdb), read_page(igdb))
    assert tokens_sent.count(FIXTURE_TOKEN) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v4/games",
        "https://api.igdb.com.evil.example/v4/games",
        "https://api.igdb.com./v4/games",
        "http://api.igdb.com/v4/games",
    ],
)
async def test__fetch_client__refuses_a_foreign_host_before_sending(url):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with build_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(ForbiddenHostError):
            await client.get(url)
        assert seen == []
        await client.get("https://images.igdb.com/igdb/image/upload/t_thumb/scaaaa.jpg")
        assert len(seen) == 1


async def test__send_capped__stops_at_the_cap():
    body = b"x" * 100

    async def stream():
        yield body[:50]
        yield body[50:]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=stream()))
    async with build_client(transport) as client:
        request = client.build_request("GET", "https://api.igdb.com/v4/games")
        with pytest.raises(ResponseTooLargeError):
            await send_capped(client, request, max_bytes=60)
        fetched = await send_capped(client, request, max_bytes=100)
        assert fetched.content == body


def test__parse_game__valid_record_and_each_skip_reason():
    first = load("games_page_1.json")[0]
    game = parse_game(first, max_year=MAX_YEAR, skips=Counter(), drops=Counter())
    assert game is not None
    assert (game.id, game.name, game.release_year, game.game_type) == (
        1001,
        "Example Quest",
        2001,
        0,
    )
    assert game.franchise is not None and game.franchise.name == "Example"
    assert [item.name for item in game.collections] == ["Example series"]
    # Only the platforms of the earliest release, not the later Switch port.
    assert game.first_release_platforms == ["PC (Microsoft Windows)", "PlayStation 4"]
    assert game.developers == ["Made Up Studio"]
    assert (game.aggregated_rating, game.aggregated_rating_count) == (84.5, 31)
    assert game.total_rating_count == 412
    first_shot = game.screenshots[0]
    assert (first_shot.image_id, first_shot.width, first_shot.height) == ("scaaaa", 1920, 1080)

    def parse(raw: object) -> tuple[Any, dict[str, int], dict[str, int]]:
        skipped: Counter[SkipReason] = Counter()
        dropped: Counter[DropReason] = Counter()
        parsed = parse_game(raw, max_year=MAX_YEAR, skips=skipped, drops=dropped)
        # `dict`, not a Counter comparison: a zero count must not be there.
        return parsed, dict(skipped), dict(dropped)

    # A record is skipped (and counted as skipped), or kept with some items dropped (counted apart).
    invalid = load("games_invalid.json")
    expected = [
        (2001, {}, {"screenshot_invalid": 3}),
        (None, {"wrong_shape": 1}, {}),
        (None, {"wrong_shape": 1}, {}),
        (None, {"wrong_shape": 1}, {}),
        (None, {"name_without_letters": 1}, {}),
        (None, {"year_out_of_range": 1}, {}),
        (None, {"year_out_of_range": 1}, {}),
        (None, {"year_out_of_range": 1}, {}),
        (None, {"wrong_shape": 1}, {}),
    ]
    assert len(invalid) == len(expected)
    for raw, (kept_id, skips, drops) in zip(invalid, expected, strict=True):
        parsed, skipped, dropped = parse(raw)
        assert (parsed.id if parsed else None) == kept_id, raw
        assert (skipped, dropped) == (skips, drops), raw
        if parsed:
            assert [shot.image_id for shot in parsed.screenshots] == ["sdaaaa"]

    # One bad nested item never costs the game: it is dropped and counted, a null list is empty.
    lenient = {record["id"]: parse(record) for record in load("games_lenient.json")}
    assert all(parsed is not None and not skipped for parsed, skipped, _ in lenient.values())
    assert {game_id: dropped for game_id, (_, _, dropped) in lenient.items()} == {
        3001: {"genre_invalid": 1},
        3002: {"company_invalid": 1, "involved_company_invalid": 1},
        3003: {},
        3004: {"platform_invalid": 1, "release_date_invalid": 1},
        3005: {"franchise_invalid": 1, "collection_invalid": 1},
    }
    assert [genre.name for genre in lenient[3001][0].genres] == ["Puzzle"]
    assert lenient[3002][0].developers == ["Real Studio"]
    assert (lenient[3003][0].screenshots, lenient[3003][0].genres) == ([], [])
    assert lenient[3004][0].first_release_platforms == []
    assert (lenient[3005][0].franchise, lenient[3005][0].collections) == (None, [])

    # The edges of 1950 to next year, to the second (UTC).
    edges = [
        (dt.datetime(1949, 12, 31, 23, 59, 59, tzinfo=dt.UTC), True),
        (dt.datetime(1950, 1, 1, tzinfo=dt.UTC), False),
        (dt.datetime(MAX_YEAR, 12, 31, 23, 59, 59, tzinfo=dt.UTC), False),
        (dt.datetime(MAX_YEAR + 1, 1, 1, tzinfo=dt.UTC), True),
    ]
    for moment, is_skipped in edges:
        parsed, skipped, _ = parse(edge_record(3999, moment))
        assert (parsed is None) is is_skipped, moment
        assert skipped == ({"year_out_of_range": 1} if is_skipped else {})


def test__content_settings__missing_credential_stops_before_any_request(monkeypatch):
    monkeypatch.delenv("IGDB_CLIENT_ID", raising=False)
    # `.env.example` ships the keys empty: an empty value is "not set".
    monkeypatch.setenv("IGDB_CLIENT_SECRET", "")

    def refuse_to_be_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a request was sent")

    with_id = ContentSettings(_env_file=None, IGDB_CLIENT_ID=CLIENT_ID)
    with pytest.raises(MissingSettingError, match="IGDB_CLIENT_SECRET is not set"):
        IgdbClient.from_settings(with_id, transport=httpx.MockTransport(refuse_to_be_called))
    with pytest.raises(MissingSettingError, match="IGDB_CLIENT_ID is not set"):
        IgdbClient.from_settings(ContentSettings(_env_file=None))

    full = ContentSettings(
        _env_file=None, IGDB_CLIENT_ID=CLIENT_ID, IGDB_CLIENT_SECRET=CLIENT_SECRET
    )
    assert CLIENT_SECRET not in repr(full) + str(full)
