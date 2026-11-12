"""Read games from IGDB (SPEC-04 AC-3 to AC-8, AC-85).

    async with IgdbClient(client_id, client_secret) as igdb:
        async for game in igdb.games():
            ...

The client gets its own Twitch token and renews it once when IGDB refuses it, stays within IGDB's
limits (4 request starts a second, 8 open requests), retries a bounded number of times, reads whole
lists by keyset paging and validates every record at the boundary. A record that fails validation
is skipped and counted (`IgdbClient.skipped`), never raised. IGDB text is data: it is stored and
shown as plain text only. Credentials and the token are never logged or put into an error.

Field and endpoint names were checked against https://api-docs.igdb.com/ on 2026-11-12:
`game_type` replaced `category` (ids: 0 main game ... 14 update), `collections` replaced
`collection`, `limit` is at most 500, and the rate limit is 4 requests a second with up to 8 open.
"""

import asyncio
import datetime as dt
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, ValidationError

from app.content.fetch import FetchedResponse, build_client, send_capped
from app.content.settings import ContentSettings
from app.core.clock import get_now

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
GAMES_URL = "https://api.igdb.com/v4/games"

PAGE_SIZE = 500
MAX_REQUESTS_PER_SECOND = 4
MAX_OPEN_REQUESTS = 8
# One request on top of this many retries is the most a single query ever sends.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
# Also the cap on a `Retry-After` from IGDB, so a header cannot stall a run for long.
MAX_BACKOFF_SECONDS = 60.0

FIRST_YEAR = 1950
# `id` always comes back. Nested names are expanded with dots (Apicalypse).
FIELDS = (
    "name,slug,first_release_date,game_type,version_parent,parent_game,"
    "franchise.name,franchises.name,collections.name,"
    "release_dates.date,release_dates.platform.name,genres.name,"
    "involved_companies.developer,involved_companies.company.name,"
    "aggregated_rating,aggregated_rating_count,total_rating_count,"
    "screenshots.image_id,screenshots.width,screenshots.height"
)


class IgdbError(Exception):
    """The run cannot go on. The message never holds a credential, a token or IGDB text."""


class IgdbAuthError(IgdbError):
    """Twitch refused the credentials, or IGDB refused a freshly renewed token."""


class IgdbUnavailable(IgdbError):
    """IGDB kept answering 429 or 5xx (or not at all) after the last retry."""


class GameType(IntEnum):
    """IGDB's `game_type` ids (the `Game Enums` table of the API docs)."""

    MAIN_GAME = 0
    DLC_ADDON = 1
    EXPANSION = 2
    BUNDLE = 3
    STANDALONE_EXPANSION = 4
    MOD = 5
    EPISODE = 6
    SEASON = 7
    REMAKE = 8
    REMASTER = 9
    EXPANDED_GAME = 10
    PORT = 11
    FORK = 12
    PACK = 13
    UPDATE = 14


class SkipReason(StrEnum):
    WRONG_SHAPE = "wrong_shape"
    NAME_WITHOUT_LETTERS = "name_without_letters"
    YEAR_OUT_OF_RANGE = "year_out_of_range"
    # Counts a dropped screenshot; the game itself is kept.
    SCREENSHOT_INVALID = "screenshot_invalid"


# Strict: a number sent as text, or text sent as a number, is a wrong shape, not a coercion.
class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)


class IgdbNamed(_Record):
    id: int
    name: str


class IgdbPlatform(_Record):
    name: str


class IgdbReleaseDate(_Record):
    date: int | None = None
    platform: IgdbPlatform | None = None


class IgdbCompany(_Record):
    name: str


class IgdbInvolvedCompany(_Record):
    developer: bool = False
    company: IgdbCompany | None = None


class IgdbScreenshot(_Record):
    # The id goes into an image URL later, so it is limited to what IGDB's ids look like. A
    # pydantic pattern searches, so it is anchored by hand.
    image_id: Annotated[str, Field(pattern=r"^[a-z0-9]+$")]
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]


class IgdbGame(_Record):
    id: Annotated[int, Field(gt=0)]
    name: Annotated[str, StringConstraints(strip_whitespace=True)]
    slug: str
    first_release_date: int | None = None
    game_type: int | None = None
    version_parent: int | None = None
    parent_game: int | None = None
    franchise: IgdbNamed | None = None
    franchises: list[IgdbNamed] = []
    collections: list[IgdbNamed] = []
    release_dates: list[IgdbReleaseDate] = []
    genres: list[IgdbNamed] = []
    involved_companies: list[IgdbInvolvedCompany] = []
    aggregated_rating: float | None = None
    aggregated_rating_count: int | None = None
    total_rating_count: int | None = None
    screenshots: list[IgdbScreenshot] = []

    @property
    def release_year(self) -> int | None:
        return release_year(self.first_release_date)

    @property
    def first_release_platforms(self) -> list[str]:
        """Platforms of the earliest dated release, in IGDB's order."""
        dated = [release for release in self.release_dates if release.date is not None]
        if not dated:
            return []
        earliest = min(release.date for release in dated if release.date is not None)
        return [
            release.platform.name
            for release in dated
            if release.date == earliest and release.platform is not None
        ]

    @property
    def developers(self) -> list[str]:
        return [
            entry.company.name
            for entry in self.involved_companies
            if entry.developer and entry.company is not None
        ]


def release_year(timestamp: int | None) -> int | None:
    """The UTC year of a unix time; a value too large for a date is `None` here and is refused as
    out of range by `parse_game`."""
    if timestamp is None:
        return None
    try:
        return dt.datetime.fromtimestamp(timestamp, dt.UTC).year
    except (OverflowError, OSError, ValueError):
        return None


def parse_game(raw: object, *, max_year: int, skips: Counter[SkipReason]) -> IgdbGame | None:
    """Validate one raw IGDB record. A bad record returns `None` and adds one count to `skips`;
    a bad screenshot is dropped and counted, and the game is kept."""
    if not isinstance(raw, dict):
        skips[SkipReason.WRONG_SHAPE] += 1
        return None
    screenshots: list[dict[str, Any]] = []
    dropped = 0
    raw_screenshots = raw.get("screenshots")
    if isinstance(raw_screenshots, list):
        for item in raw_screenshots:
            try:
                IgdbScreenshot.model_validate(item)
            except ValidationError:
                dropped += 1
            else:
                screenshots.append(item)
        raw = {**raw, "screenshots": screenshots}
    try:
        game = IgdbGame.model_validate(raw)
    except ValidationError:
        skips[SkipReason.WRONG_SHAPE] += 1
        return None
    if not any(char.isalnum() for char in game.name):
        skips[SkipReason.NAME_WITHOUT_LETTERS] += 1
        return None
    if game.first_release_date is not None:
        year = game.release_year
        if year is None or not FIRST_YEAR <= year <= max_year:
            skips[SkipReason.YEAR_OUT_OF_RANGE] += 1
            return None
    skips[SkipReason.SCREENSHOT_INVALID] += dropped
    return game


class RequestSpacer:
    """No request starts sooner than `interval` after the previous one. The clock and the sleep are
    injected so a test needs no real time."""

    def __init__(
        self,
        interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_start: float | None = None

    async def wait(self) -> None:
        # The lock is held while sleeping, so waiting callers start one after the other.
        async with self._lock:
            now = self._clock()
            if self._next_start is not None and now < self._next_start:
                await self._sleep(self._next_start - now)
                now = self._clock()
            self._next_start = now + self._interval


def retry_delay(retries_so_far: int, retry_after: str | None) -> float:
    """Seconds to wait before the next try: `Retry-After` when it is a whole number of seconds,
    else exponential backoff; never more than `MAX_BACKOFF_SECONDS`."""
    if retry_after is not None and retry_after.strip().isascii() and retry_after.strip().isdigit():
        return min(float(retry_after.strip()), MAX_BACKOFF_SECONDS)
    return min(BACKOFF_BASE_SECONDS * 2**retries_so_far, MAX_BACKOFF_SECONDS)


class IgdbClient:
    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = build_client(transport)
        self._sleep = sleep
        self._spacer = RequestSpacer(1 / MAX_REQUESTS_PER_SECOND, clock=clock, sleep=sleep)
        self._open_requests = asyncio.Semaphore(MAX_OPEN_REQUESTS)
        self._token_lock = asyncio.Lock()
        # In memory only: never written to a file, a log line or an error.
        self._token: SecretStr | None = None
        self.skipped: Counter[SkipReason] = Counter()

    @classmethod
    def from_settings(cls, settings: ContentSettings, **kwargs: Any) -> Self:
        """Stops before any request when a credential is missing (AC-3)."""
        settings.require("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET")
        assert settings.IGDB_CLIENT_ID is not None and settings.IGDB_CLIENT_SECRET is not None
        return cls(settings.IGDB_CLIENT_ID, settings.IGDB_CLIENT_SECRET, **kwargs)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    async def iter_pages(
        self, *, where: str | None = None, page_size: int = PAGE_SIZE
    ) -> AsyncIterator[list[Any]]:
        """Raw records, one list per request, in ascending id order (AC-6). Keyset paging
        (`id > last`) so a long run does not slow down the way a growing `offset` does."""
        if not 1 <= page_size <= PAGE_SIZE:
            raise ValueError(f"page_size must be 1 to {PAGE_SIZE}")
        last_id = 0
        while True:
            condition = f"id > {last_id}" if where is None else f"id > {last_id} & ({where})"
            page = await self._query(
                f"fields {FIELDS}; where {condition}; sort id asc; limit {page_size};"
            )
            if page:
                ids = [r["id"] for r in page if isinstance(r, dict) and type(r.get("id")) is int]
                # A page that cannot move the cursor would repeat forever.
                if not ids or max(ids) <= last_id:
                    raise IgdbError("IGDB returned a page with no usable ids")
                last_id = max(ids)
                yield page
            if len(page) < page_size:
                return

    async def games(
        self, *, where: str | None = None, page_size: int = PAGE_SIZE
    ) -> AsyncIterator[IgdbGame]:
        """Valid games in id order; the records that fail validation are counted in `skipped`."""
        max_year = get_now().year + 1
        async for page in self.iter_pages(where=where, page_size=page_size):
            for raw in page:
                game = parse_game(raw, max_year=max_year, skips=self.skipped)
                if game is not None:
                    yield game

    async def _query(self, body: str) -> list[Any]:
        retries = 0
        renewed = False
        while True:
            token = await self._current_token()
            try:
                response = await self._send(body, token)
            except httpx.TransportError as error:
                reason = type(error).__name__
                retry_after = None
            else:
                if response.status_code == 200:
                    return self._records(response)
                if response.status_code == 401:
                    if renewed:
                        raise IgdbAuthError("IGDB refused the token again after it was renewed")
                    renewed = True
                    await self._renew_token(stale=token)
                    continue
                if response.status_code != 429 and response.status_code < 500:
                    raise IgdbError(f"IGDB answered {response.status_code}")
                reason = f"status {response.status_code}"
                retry_after = response.headers.get("retry-after")
            if retries >= MAX_RETRIES:
                raise IgdbUnavailable(f"IGDB is unavailable: {reason}, after {retries} retries")
            delay = retry_delay(retries, retry_after)
            retries += 1
            logger.warning(
                "IGDB did not answer (%s); retry %d of %d in %.1f s",
                reason,
                retries,
                MAX_RETRIES,
                delay,
            )
            await self._sleep(delay)

    async def _send(self, body: str, token: SecretStr) -> FetchedResponse:
        request = self._http.build_request(
            "POST",
            GAMES_URL,
            content=body,
            headers={
                "Client-ID": self._client_id,
                "Authorization": f"Bearer {token.get_secret_value()}",
                "Accept": "application/json",
            },
        )
        async with self._open_requests:
            await self._spacer.wait()
            return await send_capped(self._http, request)

    @staticmethod
    def _records(response: FetchedResponse) -> list[Any]:
        try:
            records = response.json()
        except ValueError as error:
            raise IgdbError("IGDB answered with text that is not JSON") from error
        if not isinstance(records, list):
            raise IgdbError("IGDB answered with something other than a list")
        return records

    async def _current_token(self) -> SecretStr:
        async with self._token_lock:
            if self._token is None:
                self._token = await self._fetch_token()
            return self._token

    async def _renew_token(self, *, stale: SecretStr) -> None:
        async with self._token_lock:
            # Another request may have renewed it while this one waited for the lock.
            if self._token is None or self._token.get_secret_value() == stale.get_secret_value():
                self._token = await self._fetch_token()

    async def _fetch_token(self) -> SecretStr:
        # The secret goes in the form body, not the URL, so no URL log line can hold it.
        request = self._http.build_request(
            "POST",
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
                "grant_type": "client_credentials",
            },
        )
        try:
            response = await send_capped(self._http, request)
        except httpx.TransportError as error:
            raise IgdbAuthError(
                f"the Twitch token request failed: {type(error).__name__}"
            ) from None
        if response.status_code != 200:
            raise IgdbAuthError(f"Twitch refused the token request ({response.status_code})")
        try:
            token = response.json()["access_token"]
        except (ValueError, KeyError, TypeError):
            raise IgdbAuthError("Twitch answered with no access token") from None
        if not isinstance(token, str) or not token:
            raise IgdbAuthError("Twitch answered with no access token")
        return SecretStr(token)
