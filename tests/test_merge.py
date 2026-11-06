"""Guest merge (AC-55 to AC-62). The guess rules themselves are in `tests/test_game_guesser.py`;
here: the route's happy path and its request validation, then tables at the service level over
what an entry becomes (replay, rejection, the stored fields), a request with every kind of entry,
the account's game that wins, a second identical request that changes nothing (stats included),
and a lock timeout.

A fixture commits its data (`db_session.commit()` releases the test's savepoint), because a refused
merge rolls back, and the fixtures must not go with it."""

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import GameNotFoundError, ServiceUnavailableError
from app.games import daily_proof, service
from app.games.enums import ProgressStatus
from app.games.modules import MODULES
from app.games.modules.base import ProgressState, PuzzleData, TitleData
from app.games.schemas import MergeEntry, MergeRead, StatsRead
from app.games.stats import Play, compute_stats
from app.models.progress import Progress, ProgressGuess
from app.models.puzzle import Puzzle, PuzzleStatus
from app.models.user import User
from tests.conftest import SetNow
from tests.factories import (
    create_game,
    create_puzzle,
    create_titles,
    fake_sign,
    make_clue,
    make_local_storage,
)

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=10)
SLUG = "game-guesser"
URL = f"/v1/games/{SLUG}/progress/merge"
SKIP = None
UNKNOWN = 987_654
TOO_LARGE_FOR_A_KEY = 2**40
WON = ProgressStatus.WON
LOST = ProgressStatus.LOST
IN_PROGRESS = ProgressStatus.IN_PROGRESS

# Name to title id. "answer_twin" has the answer's normalized name and "again" has "w1"'s (Q9).
Ids = dict[str, int]
TITLE_NAMES = {
    "answer": "Secretname Quest",
    "answer_twin": "SECRETNAME  QUEST",
    "w1": "Portal 2",
    "again": "PORTAL  2",
    "w2": "Quake",
    "w3": "Doom",
    "w4": "Hades",
    "w5": "Celeste",
    "w6": "Braid",
}
WRONG_SIX = ["w1", "w2", "w3", "w4", "w5", "w6"]


def days_ago(count: int) -> dt.date:
    return TODAY - dt.timedelta(days=count)


# Puzzle dates of the world below. Scheduled days 0 to 4 ago are visible; the others are not.
DRAFT_DAY = days_ago(5)
FUTURE_DAY = TODAY + dt.timedelta(days=1)
BEFORE_LAUNCH_DAY = days_ago(12)
NO_PUZZLE_DAY = days_ago(8)


@dataclass
class World:
    ids: Ids
    user: User


async def new_user(db: AsyncSession, email: str) -> User:
    account = User(email=email, hashed_password="not-a-real-hash")
    db.add(account)
    await db.commit()
    return account


@pytest.fixture
async def world(db_session: AsyncSession, set_now: SetNow) -> World:
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    titles = dict(zip(TITLE_NAMES, await create_titles(db_session, *TITLE_NAMES.values())))
    for offset in range(5):
        await create_puzzle(
            db_session, game=game, answer=titles["answer"], puzzle_date=days_ago(offset)
        )
    for hidden_day, status in [
        (DRAFT_DAY, PuzzleStatus.DRAFT),
        (FUTURE_DAY, PuzzleStatus.SCHEDULED),
        (BEFORE_LAUNCH_DAY, PuzzleStatus.SCHEDULED),
    ]:
        await create_puzzle(
            db_session, game=game, answer=titles["answer"], puzzle_date=hidden_day, status=status
        )
    account = await new_user(db_session, "merge@example.com")
    return World(ids={name: title.id for name, title in titles.items()}, user=account)


# The default `proof` of `entry`: the valid daily proof of the entry's own puzzle.
VALID = "valid"


def entry(
    ids: Ids,
    puzzle_date: dt.date = TODAY,
    guesses: list[str | int | None] | None = None,
    *,
    daily: bool = True,
    variant: str = "default",
    proof: str | None = VALID,
) -> MergeEntry:
    """A merge entry. A string in `guesses` is a title name of the world. `proof` is the entry's
    `daily_proof`: `VALID` signs the entry's own puzzle, anything else is sent as it is. The body
    also carries the outcome a lying client would claim; it is not part of the contract and must
    change nothing."""
    if proof == VALID:
        proof = daily_proof.sign(SLUG, variant, puzzle_date)
    guess_ids = [ids[token] if isinstance(token, str) else token for token in guesses or []]
    return MergeEntry.model_validate(
        {
            "date": puzzle_date.isoformat(),
            "variant": variant,
            "guesses": guess_ids,
            "played_as_daily": daily,
            "daily_proof": proof,
            "status": "won",
            "finished_at": "2020-01-01T00:00:00Z",
            "origin": "play",
        }
    )


async def merge(db: AsyncSession, account: User, *entries: MergeEntry) -> MergeRead:
    return await service.merge_progress(db, slug=SLUG, now=NOW, user=account, entries=list(entries))


async def stored_games(db: AsyncSession) -> list[tuple[Any, ...]]:
    """Every stored game with its guesses, by puzzle date: the whole state a merge could change."""
    games = await db.execute(
        select(
            Progress.id,
            Puzzle.date,
            Progress.user_id,
            Progress.status,
            Progress.started_at,
            Progress.finished_at,
            Progress.counts_for_streak,
            Progress.is_test,
            Progress.origin,
        )
        .join(Puzzle, Puzzle.id == Progress.puzzle_id)
        .order_by(Puzzle.date, Progress.user_id)
    )
    guesses = await db.execute(
        select(ProgressGuess.progress_id, ProgressGuess.position, ProgressGuess.title_id).order_by(
            ProgressGuess.progress_id, ProgressGuess.position
        )
    )
    return [*(tuple(row) for row in games), *(tuple(row) for row in guesses)]


async def stored_of(db: AsyncSession, puzzle_date: dt.date) -> tuple[Progress, list[int | None]]:
    progress = (
        await db.execute(
            select(Progress)
            .join(Puzzle, Puzzle.id == Progress.puzzle_id)
            .where(Puzzle.date == puzzle_date)
        )
    ).scalar_one()
    guess_ids = (
        await db.execute(
            select(ProgressGuess.title_id)
            .where(ProgressGuess.progress_id == progress.id)
            .order_by(ProgressGuess.position)
        )
    ).scalars()
    return progress, list(guess_ids)


async def count_games(db: AsyncSession) -> int:
    return (await db.execute(select(func.count(Progress.id)))).scalar_one()


# AC-56, AC-59: each entry is replayed through the module; its status comes from the rules and
# never from the client. A row is (guesses, status stored, or the rejection code).
REPLAY_ROWS = [
    pytest.param(["answer"], WON, id="won_in_one"),
    pytest.param(["answer_twin"], WON, id="won_by_the_same_normalized_name"),
    pytest.param(["w1", SKIP, "answer"], WON, id="won_in_three_with_a_skip"),
    pytest.param(["w1", "w2"], IN_PROGRESS, id="in_progress"),
    pytest.param([SKIP], IN_PROGRESS, id="one_skip_in_progress"),
    pytest.param(WRONG_SIX, LOST, id="lost_with_six_wrong"),
    pytest.param([SKIP] * 6, LOST, id="lost_with_six_skips"),
    pytest.param([*WRONG_SIX[:5], "answer"], WON, id="won_on_the_last_guess"),
    pytest.param([], "invalid_guess_history", id="no_guesses"),
    pytest.param([*WRONG_SIX, "w1"], "invalid_guess_history", id="seven_guesses"),
    pytest.param([*WRONG_SIX, "answer"], "invalid_guess_history", id="entry_after_a_loss"),
    pytest.param(["answer", "w1"], "invalid_guess_history", id="entry_after_a_win"),
    pytest.param([UNKNOWN], "invalid_guess_history", id="unknown_title"),
    pytest.param(["w1", TOO_LARGE_FOR_A_KEY], "invalid_guess_history", id="id_too_large"),
    pytest.param(["w1", "w1"], "invalid_guess_history", id="duplicate_title"),
    pytest.param(["w1", "again"], "invalid_guess_history", id="duplicate_normalized_name"),
]


@pytest.mark.parametrize(("guesses", "expected"), REPLAY_ROWS)
async def test__merge_entry__replay_decides_the_status_or_rejects(
    db_session: AsyncSession, world: World, guesses: list[str | None], expected: str
) -> None:
    result = await merge(db_session, world.user, entry(world.ids, TODAY, guesses))

    if expected in set(ProgressStatus):
        assert (result.stored, result.already_present, result.rejected) == (1, 0, 0)
        progress, stored_ids = await stored_of(db_session, TODAY)
        assert progress.status == expected
        assert stored_ids == [
            world.ids[token] if isinstance(token, str) else token for token in guesses
        ]
        assert (progress.finished_at is None) == (expected == IN_PROGRESS)
    else:
        assert (result.stored, result.already_present, result.rejected) == (0, 0, 1)
        assert [(r.date, r.variant, r.code) for r in result.rejected_entries] == [
            (TODAY, "default", expected)
        ]
        assert await count_games(db_session) == 0


# AC-59: the entry names a variant or a puzzle that is not there for the player (the puzzle's own
# 404 rules: a draft, the future, before the launch date, no puzzle at all).
UNREACHABLE_ROWS = [
    pytest.param(TODAY, "hard", "variant_not_found", id="unknown_variant"),
    pytest.param(DRAFT_DAY, "default", "puzzle_not_found", id="draft"),
    pytest.param(FUTURE_DAY, "default", "puzzle_not_found", id="tomorrow"),
    pytest.param(BEFORE_LAUNCH_DAY, "default", "puzzle_not_found", id="before_the_launch_date"),
    pytest.param(NO_PUZZLE_DAY, "default", "puzzle_not_found", id="no_puzzle_that_day"),
]


@pytest.mark.parametrize(("puzzle_date", "variant", "code"), UNREACHABLE_ROWS)
async def test__merge_entry__a_puzzle_or_variant_that_is_not_there_is_rejected(
    db_session: AsyncSession, world: World, puzzle_date: dt.date, variant: str, code: str
) -> None:
    result = await merge(
        db_session, world.user, entry(world.ids, puzzle_date, ["answer"], variant=variant)
    )

    assert (result.stored, result.rejected) == (0, 1)
    assert [(r.date, r.variant, r.code) for r in result.rejected_entries] == [
        (puzzle_date, variant, code)
    ]
    assert await count_games(db_session) == 0


# AC-57, AC-49, R-23: what a stored entry carries. With the entry's valid proof, the flag is the
# client's `played_as_daily` whatever the date says (Q2), the origin is `merge`, and both times are
# the request's `now`.
@pytest.mark.parametrize(
    ("puzzle_offset", "daily"),
    [
        pytest.param(0, True, id="today_played_as_daily"),
        pytest.param(0, False, id="today_not_played_as_daily"),
        pytest.param(2, True, id="archive_day_played_as_daily"),
        pytest.param(2, False, id="archive_day_not_played_as_daily"),
    ],
)
async def test__merge_entry__stores_origin_flag_and_times(
    db_session: AsyncSession, world: World, puzzle_offset: int, daily: bool
) -> None:
    puzzle_date = days_ago(puzzle_offset)

    await merge(
        db_session, world.user, entry(world.ids, puzzle_date, ["w1", "answer"], daily=daily)
    )
    await merge(db_session, world.user, entry(world.ids, days_ago(3), ["w1"], daily=daily))

    progress, _ = await stored_of(db_session, puzzle_date)
    assert progress.user_id == world.user.id
    assert progress.origin == "merge"
    assert progress.counts_for_streak is daily
    assert progress.is_test is False
    assert progress.status == WON
    assert progress.started_at == NOW
    assert progress.finished_at == NOW
    running, _ = await stored_of(db_session, days_ago(3))
    assert (running.status, running.finished_at) == (IN_PROGRESS, None)
    assert running.counts_for_streak is daily


def proof_of(
    slug: str = SLUG, variant: str = "default", days_off: int = 0
) -> Callable[[dt.date], str]:
    """The proof of a puzzle that differs from the entry's own by slug, variant or `days_off`."""
    return lambda puzzle_date: daily_proof.sign(
        slug, variant, puzzle_date + dt.timedelta(days=days_off)
    )


# Q2: `played_as_daily` counts for the streak only with the valid `daily_proof` of that exact
# puzzle (slug, variant and date). A claim without one is stored as an archive play, not rejected,
# and counted in `daily_unproven`. A row is (offset, daily, proof of the entry's date, expected
# `counts_for_streak`, expected `daily_unproven`).
PROOF_ROWS = [
    pytest.param(0, True, proof_of(), True, 0, id="valid_proof_today"),
    pytest.param(2, True, proof_of(), True, 0, id="valid_proof_of_an_archive_day"),
    pytest.param(0, False, proof_of(), False, 0, id="valid_proof_but_not_played_as_daily"),
    pytest.param(0, False, lambda d: None, False, 0, id="not_daily_and_no_proof"),
    pytest.param(0, True, lambda d: None, False, 1, id="no_proof"),
    pytest.param(0, True, proof_of(days_off=-1), False, 1, id="proof_of_another_date"),
    pytest.param(2, True, proof_of(days_off=2), False, 1, id="proof_of_today_on_an_older_day"),
    pytest.param(0, True, proof_of(variant="hard"), False, 1, id="proof_of_another_variant"),
    pytest.param(0, True, proof_of(slug="other-game"), False, 1, id="proof_of_another_game"),
    pytest.param(0, True, lambda d: "not-a-proof", False, 1, id="garbage_proof"),
    pytest.param(0, True, lambda d: "", False, 1, id="empty_proof"),
    pytest.param(0, True, lambda d: "é" * 43, False, 1, id="non_ascii_proof"),
    pytest.param(
        0, True, lambda d: daily_proof.sign(SLUG, "default", d)[:-1], False, 1, id="cut_proof"
    ),
]


@pytest.mark.parametrize(("offset", "daily", "make_proof", "counts", "unproven"), PROOF_ROWS)
async def test__merge_entry__daily_claim_counts_only_with_the_proof_of_that_puzzle(
    db_session: AsyncSession,
    world: World,
    offset: int,
    daily: bool,
    make_proof: Callable[[dt.date], str | None],
    counts: bool,
    unproven: int,
) -> None:
    puzzle_date = days_ago(offset)

    result = await merge(
        db_session,
        world.user,
        entry(world.ids, puzzle_date, ["w1", "answer"], daily=daily, proof=make_proof(puzzle_date)),
    )

    # Never rejected for a bad proof: the game is stored, as an archive play.
    assert (result.stored, result.already_present, result.rejected) == (1, 0, 0)
    assert result.daily_unproven == unproven
    progress, _ = await stored_of(db_session, puzzle_date)
    assert progress.counts_for_streak is counts
    assert progress.status == WON


# `daily_unproven` counts stored entries only: of two unproven claims for one puzzle in a request,
# the first is stored and counted, the second is `already_present` and is not.
async def test__merge__an_unproven_claim_that_is_not_stored_is_not_counted_as_unproven(
    db_session: AsyncSession, world: World
) -> None:
    result = await merge(
        db_session,
        world.user,
        entry(world.ids, TODAY, ["answer"], proof=None),
        entry(world.ids, TODAY, ["w1"], proof=None),
    )

    assert (result.stored, result.already_present, result.rejected) == (1, 1, 0)
    assert result.daily_unproven == 1


# Q2: the views a guest gets carry `daily_proof` on the puzzle's own day, and only then. An
# account's views never do (its games are stored, so it never merges). A row is (route, days ago
# the puzzle is from today, signed in, expects a proof).
PROOF_VIEW_ROWS = [
    pytest.param("view", 0, False, True, id="view_today_guest"),
    pytest.param("resume", 0, False, True, id="resume_today_guest"),
    pytest.param("guess", 0, False, True, id="guess_today_guest"),
    pytest.param("view", 1, False, False, id="view_yesterday_guest"),
    pytest.param("resume", 1, False, False, id="resume_yesterday_guest"),
    pytest.param("guess", 1, False, False, id="guess_yesterday_guest"),
    pytest.param("view", 0, True, False, id="view_today_account"),
    pytest.param("resume", 0, True, False, id="resume_today_account"),
    pytest.param("guess", 0, True, False, id="guess_today_account"),
]


@pytest.mark.parametrize(("route", "offset", "is_signed_in", "has_proof"), PROOF_VIEW_ROWS)
async def test__puzzle_views__a_guest_gets_a_daily_proof_only_on_the_puzzles_own_day(
    db_session: AsyncSession,
    world: World,
    route: str,
    offset: int,
    is_signed_in: bool,
    has_proof: bool,
) -> None:
    puzzle_date = days_ago(offset)
    common: dict[str, Any] = {
        "slug": SLUG,
        "variant": None,
        "puzzle_date": puzzle_date,
        "now": NOW,
        "user": world.user if is_signed_in else None,
        "storage": make_local_storage(),
    }
    if route == "view":
        view = await service.get_puzzle_view(db_session, **common)
    elif route == "resume":
        view = await service.resume_puzzle(db_session, **common, previous=[])
    else:
        view = await service.submit_guess(
            db_session, **common, title_id=world.ids["w1"], previous=[]
        )

    if has_proof:
        assert view.daily_proof == daily_proof.sign(SLUG, "default", puzzle_date)
        assert daily_proof.verify(view.daily_proof, SLUG, "default", puzzle_date)
    else:
        assert view.daily_proof is None


# AC-59, AC-60: one entry of each kind among good ones. The bad ones are reported in request
# order and the good ones are stored. A repeat of one date in the request counts as present (R-22),
# and the account's earlier game on another date is not touched (AC-58).
async def test__merge__one_rejection_of_each_kind_still_merges_the_others(
    db_session: AsyncSession, world: World
) -> None:
    ids = world.ids
    account = world.user
    await merge(db_session, account, entry(ids, days_ago(4), ["w1"], daily=False))
    before = await stored_games(db_session)

    result = await merge(
        db_session,
        account,
        entry(ids, TODAY, ["answer"]),
        entry(ids, days_ago(1), ["w1", "w2"], variant="hard"),
        entry(ids, days_ago(1), ["w1", "w2"]),
        entry(ids, FUTURE_DAY, ["answer"]),
        entry(ids, days_ago(2), ["w1", "w1"]),
        entry(ids, days_ago(2), ["answer"]),
        entry(ids, days_ago(2), ["w2"]),  # The same date again: present, not stored twice.
        entry(ids, days_ago(3), []),
        entry(ids, days_ago(4), ["answer"]),  # The account already has this day.
    )

    assert result.model_dump(mode="json") == {
        "stored": 3,
        "already_present": 2,
        "rejected": 4,
        "daily_unproven": 0,
        "rejected_entries": [
            {"date": days_ago(1).isoformat(), "variant": "hard", "code": "variant_not_found"},
            {"date": FUTURE_DAY.isoformat(), "variant": "default", "code": "puzzle_not_found"},
            {
                "date": days_ago(2).isoformat(),
                "variant": "default",
                "code": "invalid_guess_history",
            },
            {
                "date": days_ago(3).isoformat(),
                "variant": "default",
                "code": "invalid_guess_history",
            },
        ],
    }
    after = await stored_games(db_session)
    # The account's own earlier game is the same row as before, and three games are new.
    assert before[0] in after
    assert await count_games(db_session) == 4
    assert [(await stored_of(db_session, days_ago(n)))[0].status for n in (0, 1, 2)] == [
        WON,
        IN_PROGRESS,
        WON,
    ]


# AC-58: a game the account already has, begun by playing, is never changed by an entry for the
# same puzzle, whatever the entry says (a win, another flag, other guesses).
async def test__merge__the_accounts_own_game_wins(db_session: AsyncSession, world: World) -> None:
    ids = world.ids
    other_account = await new_user(db_session, "other@example.com")
    play = await service.submit_guess(
        db_session,
        slug=SLUG,
        variant=None,
        puzzle_date=TODAY,
        now=NOW,
        user=world.user,
        storage=make_local_storage(),
        title_id=ids["w1"],
        previous=[],
    )
    assert play.counts_for_streak is True
    before = await stored_games(db_session)

    result = await merge(db_session, world.user, entry(ids, TODAY, ["answer"], daily=False))

    assert (result.stored, result.already_present, result.rejected) == (0, 1, 0)
    assert await stored_games(db_session) == before
    progress, stored_ids = await stored_of(db_session, TODAY)
    assert (progress.origin, progress.status, progress.counts_for_streak) == (
        "play",
        IN_PROGRESS,
        True,
    )
    assert stored_ids == [ids["w1"]]
    # Another account's merge for that puzzle is its own game, apart from this one.
    other = await merge(db_session, other_account, entry(ids, TODAY, ["answer"]))
    assert other.stored == 1
    assert await count_games(db_session) == 2


# AC-61, AC-52: a second identical request changes nothing, rows and stats alike, and the stats
# are those the module's rules give for the guest's own plays (the same `compute_stats` the
# frontend's guest stats must agree with). One variant's plays are passed (`compute_stats`).
async def test__merge__a_second_identical_request_changes_nothing_and_stats_match(
    db_session: AsyncSession, world: World
) -> None:
    ids = world.ids
    request = [
        entry(ids, days_ago(0), ["w1", "answer"]),  # Won in 2, a streak day.
        entry(ids, days_ago(1), ["answer"]),  # Won in 1.
        entry(ids, days_ago(2), WRONG_SIX),  # Lost; finished days count for the streak.
        entry(ids, days_ago(3), [SKIP, SKIP, "w1", "answer"], daily=False),  # Archive: no day.
        entry(ids, days_ago(4), ["w1"]),  # In progress: counts for nothing.
        entry(ids, FUTURE_DAY, ["answer"]),  # Rejected.
    ]
    guest_plays = [
        Play("default", days_ago(0), WON, 2, True),
        Play("default", days_ago(1), WON, 1, True),
        Play("default", days_ago(2), LOST, 6, True),
        Play("default", days_ago(3), WON, 4, False),
    ]
    module = MODULES[SLUG]
    expected_stats = StatsRead.model_validate(
        compute_stats(
            guest_plays,
            today=TODAY,
            max_guesses=module.max_guesses,
            variant="default",
            default_variant="default",
        ),
        from_attributes=True,
    )

    async def stats() -> StatsRead:
        return await service.get_stats(
            db_session, slug=SLUG, variant=None, now=NOW, user=world.user
        )

    first = await merge(db_session, world.user, *request)
    rows_after_first = await stored_games(db_session)
    stats_after_first = await stats()

    second = await merge(db_session, world.user, *request)
    # The same request in two chunks is the same again.
    third = await merge(db_session, world.user, *request[:3])
    fourth = await merge(db_session, world.user, *request[3:])

    # Every daily play of the guest carries the proof of its own day, so none is unproven.
    assert (first.stored, first.already_present, first.rejected) == (5, 0, 1)
    assert (second.stored, second.already_present, second.rejected) == (0, 5, 1)
    assert (first.daily_unproven, second.daily_unproven) == (0, 0)
    assert second.rejected_entries == first.rejected_entries
    assert (third.stored, third.already_present, fourth.stored, fourth.already_present) == (
        0,
        3,
        0,
        2,
    )
    assert await stored_games(db_session) == rows_after_first
    assert stats_after_first == expected_stats
    assert (stats_after_first.played, stats_after_first.wins) == (4, 3)
    assert (stats_after_first.current_streak, stats_after_first.max_streak) == (3, 3)
    assert await stats() == stats_after_first


# The game is checked before anything else: an unknown one is 404 for the whole request.
async def test__merge__an_unknown_game_is_refused_before_the_entries(
    db_session: AsyncSession, world: World
) -> None:
    with pytest.raises(GameNotFoundError):
        await service.merge_progress(
            db_session,
            slug="no-such-game",
            now=NOW,
            user=world.user,
            entries=[entry(world.ids, TODAY, ["answer"])],
        )
    assert await count_games(db_session) == 0


# AC-58 and no answer oracle: an entry for a puzzle the account already has is `already_present`
# and is never replayed, so a guest cannot ask "is X the answer?" by sending `[X, null]` (a replay
# says "invalid" after a win and "valid" otherwise, with no guess used). Whatever the guesses,
# valid or not, the response is the same.
ORACLE_ROWS = [
    pytest.param(["answer", SKIP], id="answer_then_a_skip"),
    pytest.param(["w2", SKIP], id="wrong_then_a_skip"),
    pytest.param(["answer_twin", SKIP], id="answers_twin_then_a_skip"),
    pytest.param(["answer", "answer"], id="a_duplicate_answer"),
    pytest.param(["w2", "w2"], id="a_duplicate_wrong"),
    pytest.param([UNKNOWN], id="unknown_title"),
]


@pytest.mark.parametrize("guesses", ORACLE_ROWS)
async def test__merge__an_owned_puzzle_is_present_whatever_the_guesses_say(
    db_session: AsyncSession, world: World, guesses: list[str | int | None]
) -> None:
    await service.submit_guess(
        db_session,
        slug=SLUG,
        variant=None,
        puzzle_date=TODAY,
        now=NOW,
        user=world.user,
        storage=make_local_storage(),
        title_id=world.ids["w1"],
        previous=[],
    )
    before = await stored_games(db_session)

    result = await merge(db_session, world.user, entry(world.ids, TODAY, guesses))

    assert result.model_dump(mode="json") == {
        "stored": 0,
        "already_present": 1,
        "rejected": 0,
        "daily_unproven": 0,
        "rejected_entries": [],
    }
    assert await stored_games(db_session) == before


# A game the account begins between the owned read and the replay (a guess in another tab): the
# owned read saw nothing, so the entry is replayed and fails, but the account owns the puzzle by the
# end, so it is `already_present` and not a rejection (AC-58). The window is simulated: the first
# owned read answers "nothing" while the row exists. An entry that fails on a puzzle the account
# does not own stays rejected.
async def test__merge__a_game_begun_after_the_owned_read_is_present_not_rejected(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = world.ids
    await service.submit_guess(
        db_session,
        slug=SLUG,
        variant=None,
        puzzle_date=TODAY,
        now=NOW,
        user=world.user,
        storage=make_local_storage(),
        title_id=ids["w1"],
        previous=[],
    )
    before = await stored_games(db_session)
    real_owned = service.list_owned_puzzle_ids
    reads = 0

    async def stale_first_read(*args: Any, **kwargs: Any) -> set[int]:
        nonlocal reads
        reads += 1
        if reads == 1:
            return set()
        return await real_owned(*args, **kwargs)

    monkeypatch.setattr(service, "list_owned_puzzle_ids", stale_first_read)

    result = await merge(
        db_session,
        world.user,
        entry(ids, TODAY, ["answer", SKIP]),
        entry(ids, days_ago(1), ["answer", SKIP]),
    )

    assert reads == 2
    assert result.model_dump(mode="json") == {
        "stored": 0,
        "already_present": 1,
        "rejected": 1,
        "daily_unproven": 0,
        "rejected_entries": [
            {
                "date": days_ago(1).isoformat(),
                "variant": "default",
                "code": "invalid_guess_history",
            }
        ],
    }
    assert await stored_games(db_session) == before


# The guess count and the variant are checked before the database is asked anything for the entry:
# such an entry never reaches the puzzle query, and its code is the count's even when its puzzle
# is hidden.
async def test__merge__a_bad_guess_count_or_variant_never_reaches_the_database(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = world.ids
    asked: list[set[tuple[str, dt.date]]] = []
    real_list = service.list_visible_puzzles

    async def spy(*args: Any, **kwargs: Any) -> Any:
        asked.append(set(kwargs["keys"]))
        return await real_list(*args, **kwargs)

    monkeypatch.setattr(service, "list_visible_puzzles", spy)

    result = await merge(
        db_session,
        world.user,
        entry(ids, days_ago(1), []),
        entry(ids, days_ago(2), [*WRONG_SIX, "w1"]),
        entry(ids, days_ago(3), ["answer"], variant="hard"),
        entry(ids, FUTURE_DAY, []),
        entry(ids, days_ago(4), [], variant="hard"),  # Both wrong: the count comes first.
        entry(ids, TODAY, ["answer"]),
    )

    assert asked == [{("default", TODAY)}]
    assert [(r.date, r.code) for r in result.rejected_entries] == [
        (days_ago(1), "invalid_guess_history"),
        (days_ago(2), "invalid_guess_history"),
        (days_ago(3), "variant_not_found"),
        (FUTURE_DAY, "invalid_guess_history"),
        (days_ago(4), "invalid_guess_history"),
    ]
    assert result.stored == 1


# Two merges of one account that overlap take the row locks of their inserts in the same order
# only when each stores in one order, whatever order the client sent: sorted by (variant, date).
async def test__merge__entries_are_stored_sorted_by_variant_and_date(
    db_session: AsyncSession, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = world.ids
    stored_puzzles: list[int] = []
    real_insert = service.insert_merged_progress

    async def spy(*args: Any, **kwargs: Any) -> bool:
        stored_puzzles.append(kwargs["puzzle_id"])
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(service, "insert_merged_progress", spy)

    result = await merge(
        db_session,
        world.user,
        *(entry(ids, days_ago(offset), ["w1"]) for offset in (1, 3, 0, 2)),
    )

    assert result.stored == 4
    dates = [(await db_session.get(Puzzle, puzzle_id)).date for puzzle_id in stored_puzzles]  # type: ignore[union-attr]
    assert dates == sorted(dates)


# A rollback that fails never replaces the error being handled, and its log line names the
# operation that failed ("merge"), not a guess.
async def test__merge__a_failing_rollback_is_logged_and_does_not_replace_the_error(
    db_session: AsyncSession,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken_insert(*args: Any, **kwargs: Any) -> bool:
        raise ValueError("the original error")

    async def broken_rollback() -> None:
        raise ConnectionError("the connection is gone")

    monkeypatch.setattr(service, "insert_merged_progress", broken_insert)
    monkeypatch.setattr(db_session, "rollback", broken_rollback)

    with caplog.at_level("WARNING", logger=service.logger.name), pytest.raises(ValueError):
        await merge(db_session, world.user, entry(world.ids, TODAY, ["answer"]))

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Rollback failed after an error in merge"]


class FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


# A lock timeout or a deadlock while an entry is stored (another request holds the same game) is
# 503 and nothing of the request is stored, entries before it included; any other database error
# is not hidden.
@pytest.mark.parametrize(
    ("sqlstate", "error_type"),
    [
        pytest.param("55P03", ServiceUnavailableError, id="lock_timeout"),
        pytest.param("40P01", ServiceUnavailableError, id="deadlock"),
        pytest.param("40001", DBAPIError, id="other_database_error"),
    ],
)
async def test__merge__a_database_error_stores_nothing(
    db_session: AsyncSession,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
    error_type: type[Exception],
) -> None:
    real_insert = service.insert_merged_progress
    calls = 0

    async def failing_second_insert(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DBAPIError("INSERT", {}, FakeDriverError(sqlstate))
        return await real_insert(*args, **kwargs)

    monkeypatch.setattr(service, "insert_merged_progress", failing_second_insert)

    with pytest.raises(error_type):
        await merge(
            db_session,
            world.user,
            entry(world.ids, days_ago(0), ["answer"]),
            entry(world.ids, days_ago(1), ["answer"]),
        )

    assert await count_games(db_session) == 0


async def test__merge_progress__happy_path_for_a_signed_in_account(
    client: AsyncClient, db_session: AsyncSession, world: World
) -> None:
    ids = world.ids
    # The proof comes from the view the guest got on the puzzle's own day, before signing up.
    guest_view = await client.get(f"/v1/games/{SLUG}/puzzles/today")
    proof = guest_view.json()["daily_proof"]
    assert proof is not None
    registered = await client.post(
        "/v1/auth/register", json={"email": "route@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201
    body = {
        "entries": [
            {
                "date": TODAY.isoformat(),
                "variant": "default",
                "guesses": [ids["w1"], None, ids["answer"]],
                "played_as_daily": True,
                "daily_proof": proof,
            },
            {
                "date": FUTURE_DAY.isoformat(),
                "variant": "default",
                "guesses": [ids["answer"]],
                "played_as_daily": True,
            },
        ]
    }

    response = await client.post(URL, json=body)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "stored": 1,
        "already_present": 0,
        "rejected": 1,
        "daily_unproven": 0,
        "rejected_entries": [
            {"date": FUTURE_DAY.isoformat(), "variant": "default", "code": "puzzle_not_found"}
        ],
    }
    progress, _ = await stored_of(db_session, TODAY)
    assert uuid.UUID(registered.json()["id"]) == progress.user_id
    assert progress.counts_for_streak is True


def _valid_entry(ids: Ids) -> dict[str, Any]:
    return {
        "date": TODAY.isoformat(),
        "variant": "default",
        "guesses": [ids["answer"]],
        "played_as_daily": True,
    }


# AC-55, AC-62 and the shape of the body. FastAPI answers these before the route runs, so one
# table through the client. `errors` is the list of (field, code, params); None means success.
VALIDATION_ROWS = [
    pytest.param(
        lambda e: {"entries": [e] * 501},
        [("entries", "too_long", {"max": 500})],
        id="501_entries",
    ),
    pytest.param(lambda e: {"entries": [e] * 500}, None, id="500_entries"),
    pytest.param(lambda e: {}, [("entries", "required", {})], id="entries_missing"),
    pytest.param(
        lambda e: {"entries": e}, [("entries", "invalid_type", {})], id="entries_not_a_list"
    ),
    pytest.param(
        lambda e: {"entries": [{**e, "date": "20261103"}]},
        [("entries.0.date", "invalid_value", {})],
        id="date_not_strict",
    ),
    pytest.param(
        lambda e: {"entries": [{**e, "played_as_daily": "yes"}]},
        [("entries.0.played_as_daily", "bool_type", {})],
        id="flag_not_a_boolean",
    ),
    pytest.param(
        lambda e: {"entries": [{**e, "guesses": list(range(1, 22))}]},
        [("entries.0.guesses", "too_long", {"max": 20})],
        id="21_guesses",
    ),
]


@pytest.mark.parametrize(("make_body", "errors"), VALIDATION_ROWS)
async def test__merge_progress__request_validation(
    client: AsyncClient,
    db_session: AsyncSession,
    world: World,
    make_body: Any,
    errors: list[tuple[str, str, dict[str, int]]] | None,
) -> None:
    registered = await client.post(
        "/v1/auth/register", json={"email": "route@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201

    response = await client.post(URL, json=make_body(_valid_entry(world.ids)))

    if errors is None:
        assert response.status_code == 200
        # One stored, the rest of the same date are present (and 500 entries are accepted).
        assert response.json()["stored"] == 1
        assert response.json()["already_present"] == 499
        return
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert [(e["field"], e["code"], e["params"]) for e in response.json()["errors"]] == errors
    assert await count_games(db_session) == 0


# The caps on `variant` (32, the length of `puzzles.variant`) and `daily_proof` (100): the last
# length that fits is accepted and one more is a 422. A row is (field, length, the 422's field and
# max or None, (stored, rejected) of the 200). A variant that fits but names no variant is a
# rejected entry, not a 422.
LENGTH_ROWS = [
    pytest.param("variant", 32, None, (0, 1), id="variant_32"),
    pytest.param("variant", 33, ("entries.0.variant", 32), None, id="variant_33"),
    pytest.param("daily_proof", 100, None, (1, 0), id="proof_100"),
    pytest.param("daily_proof", 101, ("entries.0.daily_proof", 100), None, id="proof_101"),
]


@pytest.mark.parametrize(("field", "length", "error", "counts"), LENGTH_ROWS)
async def test__merge_progress__the_variant_and_proof_length_caps(
    client: AsyncClient,
    db_session: AsyncSession,
    world: World,
    field: str,
    length: int,
    error: tuple[str, int] | None,
    counts: tuple[int, int] | None,
) -> None:
    registered = await client.post(
        "/v1/auth/register", json={"email": "caps@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201

    response = await client.post(
        URL, json={"entries": [{**_valid_entry(world.ids), field: "x" * length}]}
    )

    if error is None:
        assert response.status_code == 200
        assert (response.json()["stored"], response.json()["rejected"]) == counts
        return
    assert response.status_code == 422
    assert [(e["field"], e["code"], e["params"]) for e in response.json()["errors"]] == [
        (error[0], "too_long", {"max": error[1]})
    ]
    assert await count_games(db_session) == 0


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no_cookie"),
        pytest.param({"Cookie": "session_id=unknown"}, id="unknown_cookie"),
    ],
)
async def test__merge_progress__a_guest_gets_401_and_nothing_is_stored(
    client: AsyncClient, db_session: AsyncSession, world: World, headers: dict[str, str]
) -> None:
    response = await client.post(URL, json={"entries": [_valid_entry(world.ids)]}, headers=headers)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert await count_games(db_session) == 0


# The proof is compared with `hmac.compare_digest`, never with `==`: a spy sees every comparison,
# a right proof and a wrong one alike.
def test__daily_proof__verify_compares_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare = daily_proof.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(daily_proof.hmac, "compare_digest", spy)
    proof = daily_proof.sign(SLUG, "default", TODAY)

    assert daily_proof.verify(proof, SLUG, "default", TODAY) is True
    other_last = "B" if proof[-1] == "A" else "A"
    assert daily_proof.verify(proof[:-1] + other_last, SLUG, "default", TODAY) is False
    assert len(calls) == 2
    assert calls[0] == (proof.encode(), proof.encode())


# The derived key is cached by the secret's value: derived once per secret, and a changed
# `SECRET_KEY` gives other proofs (never the old key's), and the old secret gives the old ones back.
def test__daily_proof__the_key_is_cached_per_secret_value(monkeypatch: pytest.MonkeyPatch) -> None:
    original = daily_proof.sign(SLUG, "default", TODAY)
    daily_proof._derive_key.cache_clear()

    assert daily_proof.sign(SLUG, "default", TODAY) == original
    assert daily_proof.sign(SLUG, "default", TODAY - dt.timedelta(days=1)) != original
    assert daily_proof._derive_key.cache_info().misses == 1

    monkeypatch.setattr(daily_proof.settings, "SECRET_KEY", SecretStr("another-secret" * 3))
    assert daily_proof.sign(SLUG, "default", TODAY) != original
    assert daily_proof.verify(original, SLUG, "default", TODAY) is False
    assert daily_proof._derive_key.cache_info().misses == 2

    monkeypatch.undo()
    assert daily_proof.sign(SLUG, "default", TODAY) == original


# Every game's view inherits `daily_proof` from the base model, and a value set by the service is
# validated: a view built with no proof has `null`, and a wrong type is refused.
def test__game_view_base__daily_proof_defaults_to_null_and_is_validated_on_assignment() -> None:
    view = MODULES[SLUG].get_puzzle(
        _puzzle_data_for_view(), ProgressState(), is_signed_in=False, sign=fake_sign
    )

    assert view.daily_proof is None
    view.daily_proof = "a-proof"
    assert view.daily_proof == "a-proof"
    with pytest.raises(ValidationError):
        view.daily_proof = 123  # type: ignore[assignment]


def _puzzle_data_for_view() -> PuzzleData:
    return PuzzleData(
        slug=SLUG,
        variant="default",
        title_kind="video_game",
        date=TODAY,
        number=1,
        answer=TitleData(
            id=1,
            kind="video_game",
            name="Answer",
            normalized_name="answer",
            franchise=None,
            normalized_franchise=None,
            year=None,
        ),
        clues=tuple(make_clue(step) for step in range(1, 7)),
    )
