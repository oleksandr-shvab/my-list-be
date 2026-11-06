"""Stats and streak of an account (AC-46, AC-48, AC-51, AC-53). The rules themselves are the S and
T tables in `tests/test_stats_rules.py`; here: the route's happy path, the guest refusals, the
service's checks and the stored rows it reads and leaves out.

A stored game is made directly (a progress row with skips as guesses): stats read only the status,
the number of guesses, the flag and the puzzle's date, and the guess rules have their own tests."""

import datetime as dt
import logging
import uuid
from typing import NamedTuple

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, GameNotFoundError, VariantNotFoundError
from app.crud.progress import PlayRow, list_finished_plays
from app.games import service
from app.games.enums import ProgressStatus
from app.games.modules import MODULES
from app.games.modules.game_guesser import GameGuesserModule
from app.models.game import Game
from app.models.progress import Progress, ProgressGuess, ProgressOrigin
from app.models.puzzle import Puzzle, PuzzleStatus
from app.models.title import Title
from app.models.user import User
from tests.conftest import SetNow
from tests.factories import create_game, create_puzzle, create_titles

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
SLUG = "game-guesser"
OTHER_VARIANT = "hard"
WON = ProgressStatus.WON
LOST = ProgressStatus.LOST
IN_PROGRESS = ProgressStatus.IN_PROGRESS


def days_ago(count: int) -> dt.date:
    return TODAY - dt.timedelta(days=count)


class VariantModule(GameGuesserModule):
    """The game guesser's rules with a second variant, so the non-default path can be reached."""

    def has_variant(self, variant: str, *, default_variant: str) -> bool:
        return variant in (default_variant, OTHER_VARIANT)


class Stored(NamedTuple):
    puzzle: Puzzle
    progress: Progress


async def store_play(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    game: Game,
    answer: Title,
    puzzle_date: dt.date,
    status: str,
    guesses: int,
    counts_for_streak: bool = True,
    is_test: bool = False,
    variant: str | None = None,
) -> Stored:
    """A stored game on a new puzzle of `puzzle_date`, with `guesses` skips."""
    puzzle = await create_puzzle(
        db, game=game, answer=answer, puzzle_date=puzzle_date, variant=variant
    )
    progress = Progress(
        user_id=user_id,
        puzzle_id=puzzle.id,
        status=status,
        started_at=NOW,
        finished_at=None if status == IN_PROGRESS else NOW,
        counts_for_streak=counts_for_streak,
        is_test=is_test,
        origin=ProgressOrigin.PLAY,
    )
    db.add(progress)
    await db.flush()
    db.add_all(
        ProgressGuess(progress_id=progress.id, position=position, title_id=None)
        for position in range(1, guesses + 1)
    )
    await db.flush()
    return Stored(puzzle=puzzle, progress=progress)


async def new_user(db: AsyncSession, email: str) -> User:
    account = User(email=email, hashed_password="not-a-real-hash")
    db.add(account)
    await db.flush()
    return account


@pytest.fixture
async def world(db_session: AsyncSession, set_now: SetNow) -> tuple[Game, Title]:
    set_now(NOW)
    game = await create_game(db_session, launch_date=days_ago(30))
    (answer,) = await create_titles(db_session, "Secretname Quest")
    return game, answer


async def test__get_stats__happy_path_for_a_signed_in_account(
    client: AsyncClient, db_session: AsyncSession, world: tuple[Game, Title]
) -> None:
    game, answer = world
    registered = await client.post(
        "/v1/auth/register", json={"email": "stats@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201
    user_id = uuid.UUID(registered.json()["id"])
    other = await new_user(db_session, "other@example.com")

    async def store(puzzle_days_ago: int, status: str, guesses: int, **kwargs: bool) -> None:
        await store_play(
            db_session,
            user_id=user_id,
            game=game,
            answer=answer,
            puzzle_date=days_ago(puzzle_days_ago),
            status=status,
            guesses=guesses,
            **kwargs,
        )

    await store(2, WON, 3)
    await store(1, LOST, 6)
    other_game = await create_game(db_session, slug="other-game", launch_date=days_ago(30))
    await store_play(
        db_session,
        user_id=user_id,
        game=other_game,
        answer=answer,
        puzzle_date=days_ago(3),
        status=WON,
        guesses=1,
    )
    await store_play(
        db_session,
        user_id=other.id,
        game=game,
        answer=answer,
        puzzle_date=days_ago(3),
        status=WON,
        guesses=1,
    )
    url = f"/v1/games/{SLUG}/stats"

    first = await client.get(url)

    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-store"
    assert first.json() == {
        "played": 2,
        "wins": 1,
        "current_streak": 2,
        "max_streak": 2,
        "distribution": {"1": 0, "2": 0, "3": 1, "4": 0, "5": 0, "6": 0},
    }

    # Computed on every read, never stored (AC-53): a new game shows at once.
    await store(0, WON, 1)
    second = await client.get(url)

    assert second.json()["current_streak"] == 3
    assert second.json()["distribution"]["1"] == 1


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no_cookie"),
        pytest.param({"Cookie": "session_id=unknown"}, id="unknown_cookie"),
    ],
)
async def test__get_stats__guest_gets_401(
    client: AsyncClient, world: tuple[Game, Title], headers: dict[str, str]
) -> None:
    response = await client.get(f"/v1/games/{SLUG}/stats", headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


@pytest.mark.parametrize(
    ("slug", "variant", "error"),
    [
        pytest.param("no-such-game", None, GameNotFoundError, id="unknown_game"),
        pytest.param(SLUG, "no-such-variant", VariantNotFoundError, id="unknown_variant"),
        # The game guesser has one variant: its default (R-4).
        pytest.param(SLUG, OTHER_VARIANT, VariantNotFoundError, id="variant_the_game_lacks"),
    ],
)
async def test__get_stats__refuses_an_unknown_game_or_variant(
    db_session: AsyncSession,
    world: tuple[Game, Title],
    slug: str,
    variant: str | None,
    error: type[AppException],
) -> None:
    account = await new_user(db_session, "player@example.com")

    with pytest.raises(error):
        await service.get_stats(db_session, slug=slug, variant=variant, now=NOW, user=account)


async def test__get_stats__skips_an_is_test_row(
    db_session: AsyncSession, world: tuple[Game, Title]
) -> None:
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    for puzzle_days_ago, is_test in ((2, False), (1, True)):
        await store_play(
            db_session,
            user_id=account.id,
            game=game,
            answer=answer,
            puzzle_date=days_ago(puzzle_days_ago),
            status=WON,
            guesses=2,
            is_test=is_test,
        )

    stats = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)

    # AC-48: the test row is in no counter and fills no streak day.
    assert (stats.played, stats.wins, stats.current_streak, stats.max_streak) == (1, 1, 1, 1)
    assert stats.distribution[2] == 1


async def test__get_stats__other_variant_has_its_own_counters_and_no_streak(
    db_session: AsyncSession, world: tuple[Game, Title], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(MODULES, SLUG, VariantModule())
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    for variant, puzzle_days_ago, guesses in ((None, 2, 1), (OTHER_VARIANT, 1, 4)):
        await store_play(
            db_session,
            user_id=account.id,
            game=game,
            answer=answer,
            puzzle_date=days_ago(puzzle_days_ago),
            status=WON,
            guesses=guesses,
            variant=variant,
        )

    default = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)
    other = await service.get_stats(
        db_session, slug=SLUG, variant=OTHER_VARIANT, now=NOW, user=account
    )

    assert (default.played, default.current_streak, default.max_streak) == (1, 1, 1)
    assert default.distribution[1] == 1
    # AC-51: the other variant counts its own games, and its streak fields are null.
    assert (other.played, other.current_streak, other.max_streak) == (1, None, None)
    assert other.distribution[4] == 1


async def test__get_stats__ignores_a_game_in_progress(
    db_session: AsyncSession, world: tuple[Game, Title]
) -> None:
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    for puzzle_days_ago, status, guesses in ((3, WON, 2), (2, IN_PROGRESS, 4), (1, LOST, 6)):
        await store_play(
            db_session,
            user_id=account.id,
            game=game,
            answer=answer,
            puzzle_date=days_ago(puzzle_days_ago),
            status=status,
            guesses=guesses,
        )

    stats = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)
    rows = await list_finished_plays(
        db_session, user_id=account.id, game_id=game.id, variant="default", today=TODAY
    )

    # The game in progress counts for nothing, and the query does not even load it.
    # (Counted as a streak day, it would make the run 3.)
    assert (stats.played, stats.wins, stats.current_streak, stats.max_streak) == (2, 1, 2, 2)
    assert sorted(row.status for row in rows) == [LOST, WON]


@pytest.mark.parametrize(
    "hide",
    [
        pytest.param("unschedule", id="puzzle_moved_back_to_draft"),
        pytest.param("move_launch_date", id="launch_date_moved_past_the_puzzle"),
    ],
)
async def test__get_stats__skips_a_play_of_a_puzzle_that_is_no_longer_visible(
    db_session: AsyncSession, world: tuple[Game, Title], hide: str
) -> None:
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    plays = {}
    for puzzle_days_ago in (3, 2, 1):
        plays[puzzle_days_ago] = await store_play(
            db_session,
            user_id=account.id,
            game=game,
            answer=answer,
            puzzle_date=days_ago(puzzle_days_ago),
            status=WON,
            guesses=puzzle_days_ago,
        )
    before = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)
    assert (before.played, before.max_streak) == (3, 3)

    if hide == "unschedule":
        plays[2].puzzle.status = PuzzleStatus.DRAFT
    else:
        game.launch_date = days_ago(1)  # Hides the puzzles of 3 and 2 days ago.
    await db_session.flush()
    after = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)

    # AC-12: the shared visibility rule, as in the archive. The hidden play is in no counter, no
    # distribution key and no streak day.
    if hide == "unschedule":
        assert (after.played, after.wins) == (2, 2)
        assert after.distribution == {1: 1, 2: 0, 3: 1, 4: 0, 5: 0, 6: 0}
        assert (after.current_streak, after.max_streak) == (2, 2)
    else:
        assert (after.played, after.wins) == (1, 1)
        assert after.distribution == {1: 1, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
        assert (after.current_streak, after.max_streak) == (1, 1)


async def test__list_finished_plays__fills_every_field_by_name(
    db_session: AsyncSession, world: tuple[Game, Title]
) -> None:
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    stored = await store_play(
        db_session,
        user_id=account.id,
        game=game,
        answer=answer,
        puzzle_date=days_ago(2),
        status=LOST,
        guesses=6,
        counts_for_streak=False,
    )

    rows = await list_finished_plays(
        db_session, user_id=account.id, game_id=game.id, variant="default", today=TODAY
    )

    # Every field has its own value, so a column read into the wrong field shows here.
    assert rows == [
        PlayRow(
            progress_id=stored.progress.id,
            variant="default",
            puzzle_date=days_ago(2),
            status=LOST,
            guesses_used=6,
            counts_for_streak=False,
        )
    ]


@pytest.mark.parametrize("guesses", [0, 7], ids=["zero_guesses", "seven_guesses"])
async def test__get_stats__counts_a_bad_win_without_a_bucket_and_logs_a_warning(
    db_session: AsyncSession,
    world: tuple[Game, Title],
    caplog: pytest.LogCaptureFixture,
    guesses: int,
) -> None:
    game, answer = world
    account = await new_user(db_session, "player@example.com")
    stored = []
    for puzzle_days_ago, count in ((3, 2), (2, guesses)):
        stored.append(
            await store_play(
                db_session,
                user_id=account.id,
                game=game,
                answer=answer,
                puzzle_date=days_ago(puzzle_days_ago),
                status=WON,
                guesses=count,
            )
        )

    with caplog.at_level(logging.DEBUG, logger="app.games.service"):
        stats = await service.get_stats(db_session, slug=SLUG, variant=None, now=NOW, user=account)

    assert (stats.played, stats.wins, stats.current_streak, stats.max_streak) == (2, 2, 2, 2)
    assert stats.distribution == {1: 0, 2: 1, 3: 0, 4: 0, 5: 0, 6: 0}
    (record,) = caplog.records
    assert record.levelno == logging.WARNING  # Not an error: it repeats on every read.
    for found in (str(stored[1].progress.id), str(account.id), str(days_ago(2))):
        assert found in record.getMessage()
