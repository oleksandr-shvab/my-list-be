"""The IGDB client over `httpx.MockTransport` and hand-made fixtures (AC-3 to AC-8, AC-85).

No network, no database. The fixtures are in `tests/fixtures/igdb/` (see its README)."""

import asyncio
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
    MAX_OPEN_REQUESTS,
    IgdbAuthError,
    IgdbClient,
    IgdbError,
    IgdbUnavailable,
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
MAX_YEAR = 2027


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def ok(fixture: str) -> httpx.Response:
    return httpx.Response(200, json=load(fixture))


def failure(status: int, fixture: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=load(fixture), headers=headers)


def token_reply(token: str = FIXTURE_TOKEN) -> httpx.Response:
    return httpx.Response(200, json={**load("token.json"), "access_token": token})


class Server:
    """Plays the two hosts from scripts. A request that no script answers fails the test, so a
    loop that never ends is a failure and not a hang."""

    def __init__(self, games: list[httpx.Response], tokens: list[httpx.Response] | None = None):
        self.games = list(games)
        self.tokens = [token_reply()] if tokens is None else list(tokens)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        replies = {"id.twitch.tv": self.tokens, "api.igdb.com": self.games}.get(request.url.host)
        assert replies, f"unexpected request to {request.url.host}"
        return replies.pop(0)

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
            id="six 503s stop after five retries with growing waits",
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
            id="bad records are skipped, not raised",
        ),
    ],
)
async def test__igdb_client__scripted_answers(
    games, tokens, page_size, expected_ids, error, game_calls, token_calls, sleeps, caplog
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
            async for game in igdb.games(page_size=page_size):
                ids.append(game.id)
        except IgdbError as caught:
            raised = caught

    assert ids == expected_ids
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


async def test__igdb_client__keeps_the_limits():
    now = 0.0

    async def fake_sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    starts: list[float] = []
    open_now = 0
    most_open = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal open_now, most_open
        if request.url.host == "id.twitch.tv":
            return token_reply()
        starts.append(now)
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
    assert all(later - earlier >= 0.25 for earlier, later in itertools.pairwise(starts))
    assert most_open <= MAX_OPEN_REQUESTS


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
    game = parse_game(first, max_year=MAX_YEAR, skips=Counter())
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

    invalid = load("games_invalid.json")
    expected = [
        (2001, {SkipReason.SCREENSHOT_INVALID: 3}),
        (2002, {SkipReason.WRONG_SHAPE: 1}),
        (2003, {SkipReason.WRONG_SHAPE: 1}),
        (2004, {SkipReason.WRONG_SHAPE: 1}),
        (2005, {SkipReason.NAME_WITHOUT_LETTERS: 1}),
        (2006, {SkipReason.YEAR_OUT_OF_RANGE: 1}),
        (2007, {SkipReason.YEAR_OUT_OF_RANGE: 1}),
        (2008, {SkipReason.YEAR_OUT_OF_RANGE: 1}),
        (None, {SkipReason.WRONG_SHAPE: 1}),
    ]
    assert len(invalid) == len(expected)
    for raw, (game_id, skips) in zip(invalid, expected, strict=True):
        counted: Counter[SkipReason] = Counter()
        parsed = parse_game(raw, max_year=MAX_YEAR, skips=counted)
        # Only the first record is kept, and it keeps its one good screenshot.
        assert (parsed.id if parsed else None) == (2001 if game_id == 2001 else None)
        assert +counted == Counter(skips), raw
        if parsed:
            assert [s.image_id for s in parsed.screenshots] == ["sdaaaa"]


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
