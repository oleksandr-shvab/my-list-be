"""The archive (AC-63 to AC-66).

One request/response test through the route (a signed-in account), one table for what only FastAPI
enforces (`limit`, `before`), and service-level tables for the rules: paging, the clamp, never
today or the future, visibility, gaps, ordering, the per-account state and the guest's nulls.
"""

import datetime as dt
import uuid
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import GameNotFoundError, VariantNotFoundError
from app.games import service
from app.games.enums import ArchiveState, ProgressStatus
from app.games.schemas import ArchiveItem, ArchiveList, PuzzleView
from app.models.puzzle import PuzzleStatus
from app.models.user import User
from tests.conftest import SetNow
from tests.factories import create_game, create_puzzle, create_titles, make_local_storage

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=30)
SLUG = "game-guesser"
SKIP = None

# Day offsets from today of the puzzles a listing may show. Offset -4 has no puzzle at all (a gap),
# and the rows below it are hidden by the visibility rule (AC-12).
VISIBLE = [-1, -2, -3, -5, -6]


def _day(offset: int) -> dt.date:
    return TODAY + dt.timedelta(days=offset)


@dataclass
class Data:
    answer_id: int
    wrong_id: int


@pytest.fixture
async def data(db_session: AsyncSession, set_now: SetNow) -> Data:
    """The visible puzzles of `VISIBLE`, created out of date order so that an id order is not the
    date order, plus every kind of puzzle the archive must hide."""
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    answer, wrong = await create_titles(db_session, "Alpha Quest", "Beta Racer")

    async def add(offset: int, **options: object) -> None:
        await create_puzzle(
            db_session, game=game, answer=answer, puzzle_date=_day(offset), **options
        )

    for offset in (-3, -6, -1, -5, -2):
        await add(offset)
    await add(0)  # today
    await add(1)  # tomorrow
    await add(-7, status=PuzzleStatus.DRAFT)
    await add(-8, status=PuzzleStatus.IN_REVIEW)
    await add(-9, variant="hard")  # another variant of the same game
    await add(-31)  # the day before the launch date
    # Another game, on a day this game has no puzzle (a gap): it must not fill it.
    other_game = await create_game(db_session, slug="other-game", launch_date=LAUNCH)
    await create_puzzle(db_session, game=other_game, answer=answer, puzzle_date=_day(-4))
    await db_session.commit()
    return Data(answer_id=answer.id, wrong_id=wrong.id)


async def _account(db: AsyncSession) -> User:
    account = User(email=f"{uuid.uuid4()}@example.com", hashed_password="not-a-real-hash")
    db.add(account)
    await db.commit()
    return account


async def _archive(
    db: AsyncSession,
    user: User | None = None,
    *,
    before: dt.date | None = None,
    limit: int = 30,
    slug: str = SLUG,
    variant: str | None = None,
) -> ArchiveList:
    return await service.list_archive(
        db, slug=slug, variant=variant, before=before, limit=limit, now=NOW, user=user
    )


def _dates(page: ArchiveList) -> list[int]:
    """The offsets from today of a page's items."""
    return [(item.date - TODAY).days for item in page.items]


async def _play(db: AsyncSession, user: User, offset: int, guesses: list[int | None]) -> None:
    for title_id in guesses:
        await service.submit_guess(
            db,
            slug=SLUG,
            variant=None,
            puzzle_date=_day(offset),
            now=NOW,
            user=user,
            storage=make_local_storage(),
            title_id=title_id,
            previous=[],
        )


async def test__archive__happy_path_for_a_signed_in_account(
    client: AsyncClient, data: Data
) -> None:
    registered = await client.post(
        "/v1/auth/register", json={"email": "archive@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201
    yesterday = f"/v1/games/{SLUG}/puzzles/{_day(-1)}"
    played = await client.post(f"{yesterday}/guesses", json={"title_id": None, "previous": []})
    assert played.status_code == 200

    response = await client.get(f"/v1/games/{SLUG}/archive", params={"limit": 2})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "items": [
            {"date": str(_day(-1)), "number": 30, "state": "in_progress", "guesses_used": 1},
            {"date": str(_day(-2)), "number": 29, "state": "not_played", "guesses_used": 0},
        ],
        "next_before": str(_day(-2)),
    }


async def test__archive__the_default_page_is_30_days(
    client: AsyncClient, db_session: AsyncSession, set_now: SetNow
) -> None:
    """The default `limit` lives in the route, so only a request without one sees it."""
    set_now(NOW)
    game = await create_game(db_session, launch_date=TODAY - dt.timedelta(days=40))
    (answer,) = await create_titles(db_session, "Alpha Quest")
    for offset in range(-1, -32, -1):
        await create_puzzle(
            db_session, game=game, answer=answer, puzzle_date=_day(offset), with_media=False
        )
    await db_session.commit()

    response = await client.get(f"/v1/games/{SLUG}/archive")

    assert response.status_code == 200
    body = response.json()
    assert [item["date"] for item in body["items"]] == [str(_day(-n)) for n in range(1, 31)]
    assert body["next_before"] == str(_day(-30))


# Only FastAPI enforces these (AC-64). 200 rows are the edges that must be accepted. The error is
# (field, code, params), the stable codes of `CONTRACTS.md`.
QUERIES = [
    pytest.param({"limit": 1}, 200, None, id="limit_1"),
    pytest.param({"limit": 100}, 200, None, id="limit_100"),
    pytest.param({"limit": 0}, 422, ("limit", "too_small", {"min": 1}), id="limit_0"),
    pytest.param({"limit": 101}, 422, ("limit", "too_large", {"max": 100}), id="limit_101"),
    pytest.param({"limit": -1}, 422, ("limit", "too_small", {"min": 1}), id="limit_negative"),
    pytest.param({"limit": "ten"}, 422, ("limit", "invalid_type", {}), id="limit_text"),
    # Plain digits only: pydantic's lax `int` would read all of these.
    pytest.param({"limit": "1_0"}, 422, ("limit", "invalid_type", {}), id="limit_underscore"),
    pytest.param({"limit": "+5"}, 422, ("limit", "invalid_type", {}), id="limit_plus_sign"),
    pytest.param({"limit": " 7 "}, 422, ("limit", "invalid_type", {}), id="limit_spaces"),
    pytest.param({"limit": "100.0"}, 422, ("limit", "invalid_type", {}), id="limit_decimal_point"),
    pytest.param({"limit": "1e2"}, 422, ("limit", "invalid_type", {}), id="limit_exponent"),
    pytest.param({"limit": "030"}, 200, None, id="limit_leading_zero"),
    # More than 4300 digits: pydantic's `int_parsing_size`.
    pytest.param({"limit": "9" * 4301}, 422, ("limit", "invalid_type", {}), id="limit_huge_number"),
    pytest.param({"before": "2026-11-02"}, 200, None, id="before_a_date"),
    pytest.param(
        {"before": "2026-02-30"}, 422, ("before", "invalid_value", {}), id="before_not_a_day"
    ),
    pytest.param({"before": "20261102"}, 422, ("before", "invalid_value", {}), id="before_compact"),
    pytest.param(
        {"before": "1700000000"}, 422, ("before", "invalid_value", {}), id="before_timestamp"
    ),
    pytest.param({"before": ""}, 422, ("before", "invalid_value", {}), id="before_empty"),
]


@pytest.mark.parametrize(("params", "status", "error"), QUERIES)
async def test__archive__limit_and_before_validation(
    client: AsyncClient,
    data: Data,
    params: dict[str, object],
    status: int,
    error: tuple[str, str, dict[str, int]] | None,
) -> None:
    response = await client.get(f"/v1/games/{SLUG}/archive", params=params)

    assert response.status_code == status
    if error is not None:
        field, code, error_params = error
        body = response.json()
        assert body["code"] == "validation_error"
        assert body["errors"] == [{"field": field, "code": code, "params": error_params}]


# AC-63, AC-65: pages follow `next_before`; gaps are left out; the newest day comes first.
# (limit, expected offsets of each page, expected `next_before` offset of each page)
PAGES = [
    pytest.param(2, [[-1, -2], [-3, -5], [-6]], [-2, -5, None], id="two_per_page"),
    pytest.param(1, [[-1], [-2], [-3], [-5], [-6]], [-1, -2, -3, -5, None], id="one_per_page"),
    pytest.param(4, [[-1, -2, -3, -5], [-6]], [-5, None], id="four_then_one"),
    # The last page is exactly full: there is nothing more, so no cursor (the `limit + 1` read).
    pytest.param(5, [[-1, -2, -3, -5, -6]], [None], id="exactly_full_last_page"),
    pytest.param(100, [[-1, -2, -3, -5, -6]], [None], id="everything_in_one_page"),
]


@pytest.mark.parametrize(("limit", "pages", "cursors"), PAGES)
async def test__archive__pages_follow_next_before(
    db_session: AsyncSession,
    data: Data,
    limit: int,
    pages: list[list[int]],
    cursors: list[int | None],
) -> None:
    seen_pages = []
    seen_cursors: list[int | None] = []
    before = None
    while True:
        page = await _archive(db_session, before=before, limit=limit)
        seen_pages.append(_dates(page))
        seen_cursors.append(None if page.next_before is None else (page.next_before - TODAY).days)
        if page.next_before is None:
            break
        before = page.next_before

    assert seen_pages == pages
    assert seen_cursors == cursors


# AC-64, AC-65: `before` is capped at today, and `before` itself is excluded. Today, tomorrow, a
# draft, an unscheduled review, another variant and a day before the launch are never listed, so
# the open rows equal exactly `VISIBLE`.
CLAMPS = [
    pytest.param(None, VISIBLE, id="default_is_today"),
    pytest.param(0, VISIBLE, id="before_today_excludes_today"),
    pytest.param(1, VISIBLE, id="before_tomorrow_is_clamped"),
    pytest.param(400, VISIBLE, id="before_far_future_is_clamped"),
    pytest.param(-1, [-2, -3, -5, -6], id="before_yesterday_excludes_yesterday"),
    pytest.param(-3, [-5, -6], id="before_a_listed_day"),
    pytest.param(-4, [-5, -6], id="before_a_gap"),
    pytest.param(-6, [], id="before_the_oldest"),
    pytest.param(-30, [], id="before_the_launch_date"),
]


@pytest.mark.parametrize(("before_offset", "expected"), CLAMPS)
async def test__archive__before_is_clamped_and_never_lists_today_or_hidden_puzzles(
    db_session: AsyncSession, data: Data, before_offset: int | None, expected: list[int]
) -> None:
    before = None if before_offset is None else _day(before_offset)

    page = await _archive(db_session, before=before)

    assert _dates(page) == expected
    assert page.next_before is None
    assert all(item.date < TODAY for item in page.items)


async def test__archive__a_guest_gets_numbers_and_no_state(
    db_session: AsyncSession, data: Data
) -> None:
    page = await _archive(db_session)

    # Q16: days since the launch date, plus 1.
    assert page.items == [
        ArchiveItem(date=_day(offset), number=31 + offset, state=None, guesses_used=None)
        for offset in VISIBLE
    ]


async def test__archive__an_account_sees_only_its_own_stored_state(
    db_session: AsyncSession, data: Data
) -> None:
    me = await _account(db_session)
    other = await _account(db_session)
    await _play(db_session, me, -1, [data.wrong_id, SKIP])  # in progress, 2 guesses
    await _play(db_session, me, -2, [SKIP, data.answer_id])  # won, skips count
    await _play(db_session, me, -3, [SKIP] * 6)  # lost
    await _play(db_session, other, -6, [data.answer_id])  # somebody else's win
    await _play(db_session, other, -5, [SKIP])  # somebody else's start

    page = await _archive(db_session, me)

    assert [((item.date - TODAY).days, item.state, item.guesses_used) for item in page.items] == [
        (-1, ArchiveState.IN_PROGRESS, 2),
        (-2, ArchiveState.WON, 2),
        (-3, ArchiveState.LOST, 6),
        (-5, ArchiveState.NOT_PLAYED, 0),
        (-6, ArchiveState.NOT_PLAYED, 0),
    ]
    # The guest sees none of it.
    guest = await _archive(db_session)
    assert {(item.state, item.guesses_used) for item in guest.items} == {(None, None)}


async def test__archive__a_past_puzzle_plays_through_the_normal_routes_for_stats_only(
    db_session: AsyncSession, data: Data
) -> None:
    me = await _account(db_session)
    storage = make_local_storage()

    async def view(title_id: int | None) -> PuzzleView:
        return await service.submit_guess(
            db_session,
            slug=SLUG,
            variant=None,
            puzzle_date=_day(-1),
            now=NOW,
            user=me,
            storage=storage,
            title_id=title_id,
            previous=[],
        )

    first = await view(data.wrong_id)
    opened = await service.get_puzzle_view(
        db_session,
        slug=SLUG,
        variant=None,
        puzzle_date=_day(-1),
        now=NOW,
        user=me,
        storage=storage,
    )

    # AC-66: an archive play never counts for the streak, and the list shows it as started.
    assert first.counts_for_streak is False
    assert opened.counts_for_streak is False
    page = await _archive(db_session, me)
    assert (page.items[0].state, page.items[0].guesses_used) == (ArchiveState.IN_PROGRESS, 1)


async def test__archive__unknown_game_or_variant_is_not_found(
    db_session: AsyncSession, data: Data
) -> None:
    with pytest.raises(GameNotFoundError):
        await _archive(db_session, slug="no-such-game")
    with pytest.raises(VariantNotFoundError):
        await _archive(db_session, variant="hard")


def test__archive_state__is_the_progress_statuses_plus_not_played() -> None:
    """A stored status the archive cannot name would be a 500 for every account that has one."""
    assert {state.value for state in ArchiveState} == {
        *(status.value for status in ProgressStatus),
        "not_played",
    }
