"""Account progress and resume (AC-35, AC-36, AC-38, AC-41 to AC-45).

Guess rules are in `tests/test_game_guesser.py`, the guest flow in `tests/test_guesses.py`. The
concurrency tests commit rows on independent sessions and clean up in `finally`."""

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.exceptions import (
    AppException,
    DuplicateGuessError,
    PuzzleDataError,
    ServiceUnavailableError,
)
from app.crud import progress as progress_crud
from app.crud.progress import create_progress, limit_lock_wait, lock_progress
from app.crud.user import delete_user
from app.games import service
from app.games.schemas import PuzzleView
from app.models.progress import Progress, ProgressGuess
from app.models.puzzle import PuzzleClue
from app.models.title import Title
from app.models.user import User
from tests.conftest import SetNow
from tests.factories import (
    TEST_MEDIA_BASE_URL,
    create_game,
    create_puzzle,
    create_titles,
    make_local_storage,
)

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=3)
SLUG = "game-guesser"
SKIP = None
UNKNOWN = 987_654
TOO_LARGE_FOR_A_KEY = 2**40

# Same titles as `tests/test_guesses.py`: "answer_twin" has the answer's normalized name and
# "other_again" has "other"'s (Q9).
Ids = dict[str, int]
TITLE_NAMES = {
    "answer": "Secretname Quest",
    "sibling": "Origins of Secrets",
    "other": "Portal 2",
    "other_2": "Quake",
    "answer_twin": "SECRETNAME  QUEST",
    "other_again": "PORTAL  2",
}

CODE_STATUS = {
    "duplicate_guess": 422,
    "title_not_found": 422,
    "invalid_guess_history": 422,
    "puzzle_already_finished": 409,
}


def _resolve(ids: Ids, token: str | int | None) -> int | None:
    return ids[token] if isinstance(token, str) else token


@pytest.fixture
async def ids(db_session: AsyncSession, set_now: SetNow) -> Ids:
    """Puzzles for today and yesterday; the answer is "Secretname Quest"."""
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    titles = dict(zip(TITLE_NAMES, await create_titles(db_session, *TITLE_NAMES.values())))
    # Same franchise as the answer for "sibling", so a wrong guess can get `franchise`.
    for name in ("answer", "sibling"):
        titles[name].franchise = "Secretfranchise"
        titles[name].normalized_franchise = "secretfranchise"
    for offset in (0, -1):
        await create_puzzle(
            db_session,
            game=game,
            answer=titles["answer"],
            puzzle_date=TODAY + dt.timedelta(days=offset),
        )
    # Commit (release the test's savepoint): a refused account guess rolls back, and the fixture
    # data must not go with it.
    await db_session.commit()
    return {name: title.id for name, title in titles.items()}


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    return await _new_user(db_session, "player@example.com")


async def _new_user(db: AsyncSession, email: str) -> User:
    account = User(email=email, hashed_password="not-a-real-hash")
    db.add(account)
    await db.commit()
    return account


def _assert_signed_media(view: PuzzleView) -> None:
    """Every view, from any path (account or guest guess, resume, the stored view), has the signed
    links of its earned steps and no other: one image link per step, and for step 6 the clip and
    its poster (AC-71). The helpers below run it, so every table row checks it."""
    links = [source.url for step in view.steps for source in step.media.sources]
    links += [step.media.poster.url for step in view.steps if step.media.poster is not None]
    assert len(links) == len(view.steps) + (1 if len(view.steps) == 6 else 0)
    for url in links:
        assert url.startswith(f"{TEST_MEDIA_BASE_URL}/v1/media/"), url
        assert "?exp=" in url and "&sig=" in url, url
    assert view.model_dump_json().count("/v1/media/") == len(links)


async def _guess(
    db: AsyncSession,
    ids: Ids,
    user: User | None,
    token: str | int | None,
    previous: list[str | int | None] | None = None,
    *,
    now: dt.datetime = NOW,
    puzzle_date: dt.date = TODAY,
) -> PuzzleView:
    try:
        view = await service.submit_guess(
            db,
            slug=SLUG,
            variant=None,
            puzzle_date=puzzle_date,
            now=now,
            user=user,
            storage=make_local_storage(),
            title_id=_resolve(ids, token),
            previous=[_resolve(ids, entry) for entry in previous or []],
        )
        _assert_signed_media(view)
        return view
    finally:
        if user is not None and user in db:
            # A refused guess rolls the session back, which expires the test's long-lived `user`.
            # A real request ends there, so only this shared session needs the reload.
            await db.refresh(user)


async def _resume(
    db: AsyncSession,
    ids: Ids,
    user: User | None,
    previous: list[str | int | None],
    *,
    puzzle_date: dt.date = TODAY,
) -> PuzzleView:
    view = await service.resume_puzzle(
        db,
        slug=SLUG,
        variant=None,
        puzzle_date=puzzle_date,
        now=NOW,
        user=user,
        storage=make_local_storage(),
        previous=[_resolve(ids, entry) for entry in previous],
    )
    _assert_signed_media(view)
    return view


async def _view(db: AsyncSession, user: User | None, *, puzzle_date: dt.date = TODAY) -> PuzzleView:
    view = await service.get_puzzle_view(
        db,
        slug=SLUG,
        variant=None,
        puzzle_date=puzzle_date,
        now=NOW,
        user=user,
        storage=make_local_storage(),
    )
    _assert_signed_media(view)
    return view


async def _play(db: AsyncSession, ids: Ids, user: User, tokens: list[str | None]) -> None:
    for token in tokens:
        await _guess(db, ids, user, token)


async def _count_progress(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(Progress)) or 0


async def _lock_or_create(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    puzzle_id: int,
    now: dt.datetime,
    counts_for_streak: bool,
) -> Progress:
    """What the service does for a known title: limit the lock wait (as `_locked_unit` does), then
    lock the game, creating it if it has no row."""
    await limit_lock_wait(db)
    progress = await lock_progress(db, user_id=user_id, puzzle_id=puzzle_id)
    if progress is None:
        progress = await create_progress(
            db, user_id=user_id, puzzle_id=puzzle_id, now=now, counts_for_streak=counts_for_streak
        )
    return progress


async def _progress_sequence(db: AsyncSession) -> tuple[int, bool]:
    """The `progress` id sequence. Every INSERT uses a value, even one that is rolled back or
    does nothing on conflict, so an unchanged sequence proves that no INSERT ran."""
    row = (await db.execute(text("SELECT last_value, is_called FROM progress_id_seq"))).one()
    return row.last_value, row.is_called


async def _stored_guesses(db: AsyncSession, user: User) -> list[int | None]:
    result = await db.execute(
        select(ProgressGuess.title_id)
        .join(Progress, Progress.id == ProgressGuess.progress_id)
        .where(Progress.user_id == user.id)
        .order_by(ProgressGuess.position)
    )
    return list(result.scalars())


def _same_view(view: PuzzleView) -> dict[str, object]:
    """What a guest and an account must agree on (AC-44): the whole view apart from `signed_in`,
    `counts_for_streak` and `daily_proof` (a guest's proof for today's puzzle; an account has
    none)."""
    return view.model_dump(exclude={"signed_in", "counts_for_streak", "daily_proof"})


# AC-44: the same sequence of guesses, played as a guest (with a growing `previous`) and as an
# account (stored), gives the same view after every step, and the same view on a resume.
SEQUENCES = [
    pytest.param([SKIP, "other", "answer"], id="won_on_the_third"),
    pytest.param(["sibling", "answer"], id="franchise_then_win"),
    pytest.param(["answer_twin"], id="win_by_the_answer_twin"),
    pytest.param([SKIP, "other", "sibling", SKIP, "other_2", SKIP], id="lost_on_the_sixth"),
    pytest.param(["other", SKIP, "other_2"], id="stopped_in_progress"),
]


@pytest.mark.parametrize("tokens", SEQUENCES)
async def test__guest_and_account__same_guesses_give_the_same_view(
    db_session: AsyncSession, ids: Ids, user: User, tokens: list[str | None]
) -> None:
    previous: list[str | None] = []
    for token in tokens:
        guest = await _guess(db_session, ids, None, token, previous)
        account = await _guess(db_session, ids, user, token)

        assert guest.signed_in is False
        assert account.signed_in is True
        assert _same_view(guest) == _same_view(account)
        previous.append(token)

    guest_resume = await _resume(db_session, ids, None, previous)
    account_resume = await _resume(db_session, ids, user, [])
    assert _same_view(guest_resume) == _same_view(account_resume) == _same_view(account)
    assert _same_view(await _view(db_session, user)) == _same_view(account)


# AC-41: the stored game wins. Whatever `previous` says, it is not read.
FORGED_PREVIOUS = [
    pytest.param([SKIP] * 5, id="five_skips"),
    pytest.param([SKIP] * 20, id="twenty_skips"),
    pytest.param(["answer"], id="a_win"),
    pytest.param([UNKNOWN, UNKNOWN], id="unknown_titles"),
    pytest.param(["sibling", "sibling"], id="duplicates_and_the_new_guess_again"),
    pytest.param([], id="empty"),
]


@pytest.mark.parametrize("forged", FORGED_PREVIOUS)
async def test__account_guess__forged_previous_is_ignored(
    db_session: AsyncSession, ids: Ids, user: User, forged: list[str | int | None]
) -> None:
    await _play(db_session, ids, user, ["other", SKIP])

    view = await _guess(db_session, ids, user, "sibling", forged)

    assert (view.status, view.guesses_used) == ("in_progress", 3)
    assert [entry.title_id for entry in view.guesses] == [ids["other"], None, ids["sibling"]]
    assert [step.step for step in view.steps] == [1, 2, 3, 4]
    assert view.answer is None
    assert await _stored_guesses(db_session, user) == [ids["other"], None, ids["sibling"]]
    resumed = await _resume(db_session, ids, user, forged)
    assert [entry.title_id for entry in resumed.guesses] == [ids["other"], None, ids["sibling"]]
    assert await _stored_guesses(db_session, user) == [ids["other"], None, ids["sibling"]]


# The account's checks, in the guest's order minus the history: finished 409, unknown title 422,
# duplicate 422 (AC-31 to AC-33). A refused guess stores nothing.
ACCOUNT_ROWS = [
    pytest.param([], "other", None, id="first_guess"),
    pytest.param([], SKIP, None, id="first_skip"),
    pytest.param(["other", SKIP], SKIP, None, id="skips_are_not_duplicates"),
    pytest.param(["other"], "other", "duplicate_guess", id="duplicate"),
    pytest.param(["other"], "other_again", "duplicate_guess", id="duplicate_by_name"),
    pytest.param(["other_again"], "other", "duplicate_guess", id="duplicate_by_name_reversed"),
    pytest.param([], UNKNOWN, "title_not_found", id="unknown_title_creates_no_row"),
    pytest.param(["other"], UNKNOWN, "title_not_found", id="unknown_title_later"),
    pytest.param([], 0, "title_not_found", id="title_zero"),
    pytest.param([], TOO_LARGE_FOR_A_KEY, "title_not_found", id="title_beyond_a_key"),
    pytest.param(["answer"], "other", "puzzle_already_finished", id="after_a_win"),
    pytest.param(["answer"], SKIP, "puzzle_already_finished", id="skip_after_a_win"),
    pytest.param(["answer"], UNKNOWN, "puzzle_already_finished", id="finished_before_unknown"),
    pytest.param(["answer"], "answer", "puzzle_already_finished", id="finished_before_duplicate"),
    pytest.param([SKIP] * 6, SKIP, "puzzle_already_finished", id="after_a_loss"),
    pytest.param([SKIP] * 6, UNKNOWN, "puzzle_already_finished", id="after_a_loss_unknown"),
]


@pytest.mark.parametrize(("stored", "token", "code"), ACCOUNT_ROWS)
async def test__account_guess__error_order_and_nothing_stored_on_refusal(
    db_session: AsyncSession,
    ids: Ids,
    user: User,
    stored: list[str | None],
    token: str | int | None,
    code: str | None,
) -> None:
    await _play(db_session, ids, user, stored)
    before = await _stored_guesses(db_session, user)
    rows_before = await _count_progress(db_session)
    sequence_before = await _progress_sequence(db_session)

    if code is None:
        view = await _guess(db_session, ids, user, token)
        assert view.guesses_used == len(stored) + 1  # AC-30: skips count.
        assert await _stored_guesses(db_session, user) == [*before, _resolve(ids, token)]
        return

    with pytest.raises(AppException) as error:
        await _guess(db_session, ids, user, token)
    assert (error.value.code, error.value.status_code) == (code, CODE_STATUS[code])
    assert await _stored_guesses(db_session, user) == before
    assert await _count_progress(db_session) == rows_before
    # No INSERT ran at all: a refused guess never creates a game, not even one that is rolled back.
    assert await _progress_sequence(db_session) == sequence_before


STORED_ROWS = [
    pytest.param([SKIP, "answer"], "won", True, id="won"),
    pytest.param([SKIP] * 6, "lost", True, id="lost"),
    pytest.param(["sibling"], "in_progress", False, id="franchise_is_not_a_win"),
    pytest.param(["other", SKIP], "in_progress", False, id="in_progress"),
]


@pytest.mark.parametrize(("tokens", "status", "is_finished"), STORED_ROWS)
async def test__account_guess__stores_status_and_finish_time(
    db_session: AsyncSession,
    ids: Ids,
    user: User,
    tokens: list[str | None],
    status: str,
    is_finished: bool,
) -> None:
    await _play(db_session, ids, user, tokens)

    progress = (await db_session.execute(select(Progress))).scalar_one()

    assert progress.status == status
    assert progress.finished_at == (NOW if is_finished else None)
    assert progress.started_at == NOW
    assert progress.origin == "play"
    assert progress.is_test is False  # AC-43: never set.


async def test__account_guess__a_puzzle_that_cannot_be_shown_stores_nothing(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    # The view is built before the commit, so a broken puzzle (a missing step) rolls the guess back.
    await db_session.execute(delete(PuzzleClue).where(PuzzleClue.step == 6))
    await db_session.commit()

    with pytest.raises(PuzzleDataError) as error:
        await _guess(db_session, ids, user, "other")

    assert error.value.status_code == 500
    assert await _count_progress(db_session) == 0
    assert await _stored_guesses(db_session, user) == []


async def test__account_guess__finished_comes_from_the_stored_guesses_not_the_status_column(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    # One source of truth: the state rebuilt from the guesses. A status column that disagrees
    # (here "in_progress" on a won game) must not change the answer.
    await _play(db_session, ids, user, [SKIP, "answer"])
    await db_session.execute(text("UPDATE progress SET status = 'in_progress'"))

    for token in (UNKNOWN, "other", SKIP):
        with pytest.raises(AppException) as error:
            await _guess(db_session, ids, user, token)
        assert error.value.code == "puzzle_already_finished"


async def test__account_guess__finished_at_is_never_before_started_at(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    await _guess(db_session, ids, user, SKIP)
    earlier = NOW - dt.timedelta(hours=1)  # A clock that stepped back.

    await _guess(db_session, ids, user, "answer", now=earlier)

    progress = (await db_session.execute(select(Progress))).scalar_one()
    assert progress.status == "won"
    assert progress.finished_at == progress.started_at == NOW


async def test__account_guess__a_failing_rollback_does_not_replace_the_error(
    db_session: AsyncSession, ids: Ids, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _play(db_session, ids, user, ["other"])

    async def broken_rollback() -> None:
        raise ConnectionError("the connection is gone")

    monkeypatch.setattr(db_session, "rollback", broken_rollback)

    with pytest.raises(AppException) as error:
        await _guess(db_session, ids, user, "other")
    assert error.value.code == "duplicate_guess"


# AC-36 (S18, S19): the flag is set by the first stored guess, from the server clock at that
# moment, and no later request changes it. Each step is (now, puzzle date offset).
D = dt.datetime(2026, 11, 3, tzinfo=dt.UTC)
FLAG_ROWS = [
    pytest.param(
        [(D.replace(hour=23, minute=59, second=59), 0), (D + dt.timedelta(days=1, seconds=1), 0)],
        True,
        id="s19_first_before_midnight_last_after",
    ),
    pytest.param(
        [(D + dt.timedelta(days=1, seconds=1), 0), (D + dt.timedelta(days=1, hours=5), 0)],
        False,
        id="s18_first_after_midnight",
    ),
    pytest.param([(D, 0)], True, id="first_second_of_the_day"),
    pytest.param(
        [(D.replace(hour=12), -1), (D.replace(hour=13), -1)], False, id="archive_puzzle_from_today"
    ),
    pytest.param(
        [(D + dt.timedelta(days=1), -1)], False, id="midnight_makes_yesterdays_puzzle_archive"
    ),
]


@pytest.mark.parametrize(("steps", "expected"), FLAG_ROWS)
async def test__account_guess__counts_for_streak_is_set_once_by_the_first_guess(
    db_session: AsyncSession,
    ids: Ids,
    set_now: SetNow,
    user: User,
    steps: list[tuple[dt.datetime, int]],
    expected: bool,
) -> None:
    for now, offset in steps:
        puzzle_date = TODAY + dt.timedelta(days=offset)
        set_now(now)
        view = await _guess(db_session, ids, user, SKIP, now=now, puzzle_date=puzzle_date)

        assert view.counts_for_streak is expected
        progress = (await db_session.execute(select(Progress))).scalar_one()
        assert progress.counts_for_streak is expected
        assert (
            await _view(db_session, user, puzzle_date=puzzle_date)
        ).counts_for_streak is expected


# The view, today's route and resume read the stored row, and create nothing (AC-38).
READ_ROWS = [
    pytest.param([], "in_progress", None, id="no_row_yet"),
    pytest.param(["other", SKIP], "in_progress", True, id="in_progress"),
    pytest.param([SKIP, "answer"], "won", True, id="won"),
    pytest.param([SKIP] * 6, "lost", True, id="lost"),
]


@pytest.mark.parametrize(("stored", "status", "flag"), READ_ROWS)
async def test__account_reads__show_the_stored_game_and_create_no_row(
    db_session: AsyncSession,
    ids: Ids,
    user: User,
    stored: list[str | None],
    status: str,
    flag: bool | None,
) -> None:
    await _play(db_session, ids, user, stored)
    rows = await _count_progress(db_session)
    guesses = await _stored_guesses(db_session, user)

    for view in (await _view(db_session, user), await _resume(db_session, ids, user, [SKIP] * 5)):
        assert (view.status, view.guesses_used) == (status, len(stored))
        assert view.counts_for_streak is flag
        assert view.signed_in is True
        assert (view.answer is not None) == (status != "in_progress")
        expected_steps = 6 if status != "in_progress" else len(stored) + 1
        assert len(view.steps) == expected_steps

    assert await _count_progress(db_session) == rows
    assert await _stored_guesses(db_session, user) == guesses


async def test__account_view__another_account_and_another_puzzle_are_separate(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    other_user = await _new_user(db_session, "other@example.com")
    await _play(db_session, ids, user, ["other", SKIP])
    await _guess(db_session, ids, user, SKIP, puzzle_date=TODAY - dt.timedelta(days=1))

    assert (await _view(db_session, other_user)).guesses_used == 0
    assert (await _view(db_session, user)).guesses_used == 2
    assert (
        await _view(db_session, user, puzzle_date=TODAY - dt.timedelta(days=1))
    ).guesses_used == 1


# AC-38 and AC-39: a guest's resume replays `previous` (up to 6 entries, a whole lost game) and
# changes nothing.
RESUME_ROWS = [
    pytest.param([], None, "in_progress", id="empty"),
    pytest.param([SKIP, "other"], None, "in_progress", id="two_guesses"),
    pytest.param([SKIP, "other", "answer"], None, "won", id="won"),
    pytest.param(["answer_twin"], None, "won", id="won_by_the_twin"),
    pytest.param([SKIP] * 6, None, "lost", id="six_skips_a_whole_lost_game"),
    pytest.param(
        ["other", SKIP, "sibling", SKIP, "other_2", SKIP], None, "lost", id="six_mixed_lost"
    ),
    pytest.param([SKIP] * 5 + ["answer"], None, "won", id="won_on_the_sixth"),
    pytest.param([SKIP] * 7, "invalid_guess_history", None, id="seven_entries"),
    pytest.param([UNKNOWN], "invalid_guess_history", None, id="unknown_title"),
    pytest.param([TOO_LARGE_FOR_A_KEY], "invalid_guess_history", None, id="beyond_a_key"),
    pytest.param(["other", "other"], "invalid_guess_history", None, id="duplicate"),
    pytest.param(["other", "other_again"], "invalid_guess_history", None, id="duplicate_by_name"),
    pytest.param(["answer", "other"], "invalid_guess_history", None, id="entry_after_a_win"),
    pytest.param(["answer", SKIP], "invalid_guess_history", None, id="skip_after_a_win"),
]


@pytest.mark.parametrize(("previous", "code", "status"), RESUME_ROWS)
async def test__guest_resume__replays_previous_and_changes_nothing(
    db_session: AsyncSession,
    ids: Ids,
    previous: list[str | int | None],
    code: str | None,
    status: str | None,
) -> None:
    if code is not None:
        with pytest.raises(AppException) as error:
            await _resume(db_session, ids, None, previous)
        assert (error.value.code, error.value.status_code) == (code, 422)
        return

    view = await _resume(db_session, ids, None, previous)

    assert view.status == status
    assert view.guesses_used == len(previous)
    assert view.signed_in is False
    assert view.counts_for_streak is None
    assert (view.answer is not None) == (status != "in_progress")
    assert await _count_progress(db_session) == 0
    assert not db_session.new
    assert not db_session.dirty


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        pytest.param(
            {"puzzle_date": TODAY + dt.timedelta(days=1)}, "puzzle_not_found", id="future"
        ),
        pytest.param({"slug": "no-such-game"}, "game_not_found", id="game"),
        pytest.param({"variant": "nope"}, "variant_not_found", id="variant"),
    ],
)
async def test__resume__hidden_puzzle_variant_or_game_is_refused_before_previous(
    db_session: AsyncSession, ids: Ids, user: User, kwargs: dict[str, object], code: str
) -> None:
    arguments: dict[str, object] = {
        "slug": SLUG,
        "variant": None,
        "puzzle_date": TODAY,
        "now": NOW,
        "storage": make_local_storage(),
        "previous": [UNKNOWN],  # An invalid history, so only the lookup can refuse it first.
        **kwargs,
    }
    for account in (None, user):
        with pytest.raises(AppException) as error:
            await service.resume_puzzle(db_session, user=account, **arguments)  # type: ignore[arg-type]
        assert error.value.code == code


async def test__guest_guess__writes_no_progress_row(db_session: AsyncSession, ids: Ids) -> None:
    # AC-37: a guest's guess, first and later, stores nothing at all.
    await _guess(db_session, ids, None, "other")
    await _guess(db_session, ids, None, "answer", ["other"])

    assert await _count_progress(db_session) == 0
    assert await db_session.scalar(select(func.count()).select_from(ProgressGuess)) == 0


# AC-35 and AC-11 on real Postgres: each rejected row names the constraint it must hit.
async def test__progress_rows__database_rules(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    puzzle_id = await db_session.scalar(text("SELECT id FROM puzzles ORDER BY date DESC LIMIT 1"))
    other_user = await _new_user(db_session, "second@example.com")

    def progress(owner: User, **overrides: object) -> Progress:
        values: dict[str, object] = {
            "user_id": owner.id,
            "puzzle_id": puzzle_id,
            "status": "in_progress",
            "started_at": NOW,
            "counts_for_streak": True,
            "origin": "play",
        }
        return Progress(**{**values, **overrides})

    async def rejected(row: object, constraint: str) -> None:
        with pytest.raises(IntegrityError, match=constraint):
            async with db_session.begin_nested():
                db_session.add(row)
                await db_session.flush()

    first = progress(user)
    db_session.add(first)
    await db_session.flush()
    assert first.is_test is False

    await rejected(progress(user), "uq_progress_user_id_puzzle_id")
    await rejected(progress(other_user, status="paused"), "ck_progress_status")
    await rejected(progress(other_user, origin="import"), "ck_progress_origin")
    await rejected(progress(other_user, user_id=uuid.UUID(int=0)), "fk_progress_user_id")
    await rejected(progress(other_user, puzzle_id=UNKNOWN), "fk_progress_puzzle_id")

    db_session.add(ProgressGuess(progress_id=first.id, position=1, title_id=ids["other"]))
    db_session.add(ProgressGuess(progress_id=first.id, position=2, title_id=None))
    db_session.add(ProgressGuess(progress_id=first.id, position=3, title_id=None))
    await db_session.flush()
    await rejected(
        ProgressGuess(progress_id=first.id, position=1, title_id=ids["sibling"]),
        "uq_progress_guesses_progress_id_position",
    )
    await rejected(
        ProgressGuess(progress_id=first.id, position=4, title_id=ids["other"]),
        "uq_progress_guesses_progress_id_title_id",
    )
    await rejected(
        ProgressGuess(progress_id=first.id, position=0, title_id=None),
        "ck_progress_guesses_position_positive",
    )
    # AC-11: a title that a stored guess refers to cannot be deleted.
    with pytest.raises(IntegrityError, match="fk_progress_guesses_title_id_titles"):
        async with db_session.begin_nested():
            await db_session.execute(delete(Title).where(Title.id == ids["other"]))


async def test__delete_user__deletes_its_progress_and_guesses(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    survivor = await _new_user(db_session, "survivor@example.com")
    await _play(db_session, ids, user, ["other", SKIP])
    await _play(db_session, ids, survivor, ["sibling"])

    await delete_user(db_session, user)  # AC-35

    assert await _count_progress(db_session) == 1
    assert await _stored_guesses(db_session, survivor) == [ids["sibling"]]
    assert await db_session.scalar(select(func.count()).select_from(ProgressGuess)) == 1
    assert await db_session.get(Title, ids["other"]) is not None


async def test__get_or_create_progress__second_call_changes_nothing(
    db_session: AsyncSession, ids: Ids, user: User
) -> None:
    puzzle_id = await db_session.scalar(text("SELECT id FROM puzzles ORDER BY date DESC LIMIT 1"))
    later = NOW + dt.timedelta(days=1)

    first = await _lock_or_create(
        db_session, user_id=user.id, puzzle_id=puzzle_id, now=NOW, counts_for_streak=True
    )
    second = await _lock_or_create(
        db_session, user_id=user.id, puzzle_id=puzzle_id, now=later, counts_for_streak=False
    )

    assert second.id == first.id
    assert (second.counts_for_streak, second.started_at) == (True, NOW)  # AC-36: never updated.
    assert await _count_progress(db_session) == 1


async def test__resume__happy_path_for_a_signed_in_account(client: AsyncClient, ids: Ids) -> None:
    registered = await client.post(
        "/v1/auth/register", json={"email": "route@example.com", "password": "correct horse"}
    )
    assert registered.status_code == 201
    url = f"/v1/games/{SLUG}/puzzles/{TODAY}"
    for token in ("other", SKIP):
        guess = await client.post(
            f"{url}/guesses", json={"title_id": _resolve(ids, token), "previous": []}
        )
        assert guess.status_code == 200
        assert guess.json()["signed_in"] is True

    # A forged `previous` of five skips changes nothing (AC-41).
    response = await client.post(f"{url}/resume", json={"previous": [None] * 5})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["type"] == "game_guesser"
    assert body["signed_in"] is True
    assert body["status"] == "in_progress"
    assert [entry["title_id"] for entry in body["guesses"]] == [ids["other"], None]
    assert [step["step"] for step in body["steps"]] == [1, 2, 3]
    assert body["counts_for_streak"] is True
    assert body["answer"] is None
    # No internal id and no `is_test` (AC-5, AC-43): the keys are exactly the view's.
    assert set(body) == set(PuzzleView.model_fields)


# AC-45. These tests commit for real, on independent sessions, so they cannot use `client` or
# `db_session` (one connection, one savepoint). The fixture empties the tables in `finally`.
@dataclass
class Committed:
    sessions: async_sessionmaker[AsyncSession]
    ids: Ids
    puzzle_id: int

    async def new_user(self) -> User:
        async with self.sessions() as session:
            account = await _new_user(session, f"{uuid.uuid4()}@example.com")
            await session.commit()
            return account

    async def guess(self, account: User, token: str | None) -> PuzzleView:
        """One request on its own session, as a real request has."""
        async with self.sessions() as session:
            return await _guess(session, self.ids, account, token)


@pytest.fixture
async def committed(engine: AsyncEngine) -> AsyncGenerator[Committed, None]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            game = await create_game(session, launch_date=LAUNCH)
            titles = dict(zip(TITLE_NAMES, await create_titles(session, *TITLE_NAMES.values())))
            puzzle = await create_puzzle(
                session, game=game, answer=titles["answer"], puzzle_date=TODAY
            )
            await session.commit()
            committed_ids = {name: title.id for name, title in titles.items()}
        yield Committed(sessions, committed_ids, puzzle.id)
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE progress_guesses, progress, clue_media_files, puzzle_clues, puzzles, titles, games, "
                    "users RESTART IDENTITY CASCADE"
                )
            )


ROUNDS = 5  # The race is timing-dependent, so each case runs it on several fresh accounts.


async def _at_once(
    committed: Committed, account: User, *tokens: str | None
) -> list[PuzzleView | BaseException]:
    return await asyncio.gather(
        *(committed.guess(account, token) for token in tokens), return_exceptions=True
    )


async def _positions(committed: Committed, account: User) -> list[tuple[int, int | None]]:
    async with committed.sessions() as session:
        result = await session.execute(
            select(ProgressGuess.position, ProgressGuess.title_id)
            .join(Progress, Progress.id == ProgressGuess.progress_id)
            .where(Progress.user_id == account.id)
            .order_by(ProgressGuess.position)
        )
        return [(row.position, row.title_id) for row in result]


@pytest.mark.parametrize("has_row", [False, True], ids=["first_guesses", "later_guesses"])
async def test__concurrent_account_guesses__three_skips_are_all_stored_in_order(
    committed: Committed, has_row: bool
) -> None:
    for _ in range(ROUNDS):
        account = await committed.new_user()
        if has_row:
            await committed.guess(account, "other")
        offset = 1 if has_row else 0

        # Three, not two: a pair passes even when the re-select after the insert takes no lock
        # (the mutant that proved it); a third request reaches the game while the second works.
        results = await _at_once(committed, account, SKIP, SKIP, SKIP)

        assert all(isinstance(result, PuzzleView) for result in results), results
        assert sorted(result.guesses_used for result in results) == [  # type: ignore[union-attr]
            offset + 1,
            offset + 2,
            offset + 3,
        ]
        stored = await _positions(committed, account)
        assert [position for position, _ in stored] == list(range(1, offset + 4))
        assert [title_id for _, title_id in stored][offset:] == [None, None, None]


@pytest.mark.parametrize("has_row", [False, True], ids=["first_guesses", "later_guesses"])
async def test__concurrent_account_guesses__the_same_title_is_stored_once(
    committed: Committed, has_row: bool
) -> None:
    for _ in range(ROUNDS):
        account = await committed.new_user()
        if has_row:
            await committed.guess(account, SKIP)

        results = await _at_once(committed, account, "other", "other")

        views = [result for result in results if isinstance(result, PuzzleView)]
        errors = [result for result in results if not isinstance(result, PuzzleView)]
        assert len(views) == 1, results
        assert len(errors) == 1 and isinstance(errors[0], DuplicateGuessError), results
        stored = await _positions(committed, account)
        assert [title_id for _, title_id in stored].count(committed.ids["other"]) == 1
        assert [position for position, _ in stored] == list(range(1, len(stored) + 1))


@pytest.mark.parametrize("has_row", [False, True], ids=["row_not_yet_committed", "row_exists"])
async def test__progress_row__a_second_request_waits_for_the_first_to_commit(
    committed: Committed, has_row: bool
) -> None:
    # Deterministic race: one session holds the game's lock and the other request must not get past
    # it until the first commits. With a new row, the second INSERT meets the uncommitted row and
    # must neither fail nor duplicate it.
    account = await committed.new_user()
    if has_row:
        await committed.guess(account, SKIP)
    async with committed.sessions() as holder:
        await _lock_or_create(
            holder,
            user_id=account.id,
            puzzle_id=committed.puzzle_id,
            now=NOW,
            counts_for_streak=True,
        )
        second = asyncio.create_task(committed.guess(account, "other"))

        done, _ = await asyncio.wait({second}, timeout=0.5)
        assert not done, "the second request ran while the first held the game's lock"

        await holder.commit()
    view = await asyncio.wait_for(second, timeout=5)

    assert view.guesses_used == (2 if has_row else 1)
    stored = await _positions(committed, account)
    assert [position for position, _ in stored] == list(range(1, len(stored) + 1))


# A refused guess ends its transaction: no lock stays on the game and no empty game is left. The
# refusing session stays open, so only a rollback (not just a closed session) can pass this.
REFUSED_ROWS = [
    pytest.param(["other"], "other", "duplicate_guess", id="duplicate_on_a_stored_game"),
    pytest.param(["answer"], "other", "puzzle_already_finished", id="finished_stored_game"),
    pytest.param([], UNKNOWN, "title_not_found", id="unknown_title_on_a_first_guess"),
]


@pytest.mark.parametrize(("stored", "token", "code"), REFUSED_ROWS)
async def test__refused_account_guess__releases_the_lock_and_leaves_no_row(
    committed: Committed, stored: list[str | None], token: str | int | None, code: str
) -> None:
    account = await committed.new_user()
    for earlier in stored:
        await committed.guess(account, earlier)

    async with committed.sessions() as refused:
        with pytest.raises(AppException) as error:
            await _guess(refused, committed.ids, account, token)
        assert error.value.code == code

        # While `refused` is still open, another request takes the game's lock at once: it neither
        # waits on a row lock nor on an uncommitted insert.
        async with committed.sessions() as other:
            progress = await asyncio.wait_for(
                _lock_or_create(
                    other,
                    user_id=account.id,
                    puzzle_id=committed.puzzle_id,
                    now=NOW,
                    counts_for_streak=True,
                ),
                timeout=2,
            )
            assert len(progress.guesses) == len(stored)


@pytest.mark.parametrize("has_row", [False, True], ids=["row_not_yet_committed", "row_exists"])
async def test__progress_lock__waiting_too_long_gives_service_unavailable(
    committed: Committed, monkeypatch: pytest.MonkeyPatch, has_row: bool
) -> None:
    monkeypatch.setattr(progress_crud, "LOCK_TIMEOUT_MS", 300)
    account = await committed.new_user()
    if has_row:
        await committed.guess(account, SKIP)
    async with committed.sessions() as holder:
        await _lock_or_create(
            holder,
            user_id=account.id,
            puzzle_id=committed.puzzle_id,
            now=NOW,
            counts_for_streak=True,
        )
        started = time.monotonic()

        with pytest.raises(ServiceUnavailableError) as error:
            await committed.guess(account, "other")

        waited = time.monotonic() - started
    assert (error.value.status_code, error.value.code) == (503, "service_unavailable")
    assert 0.25 <= waited < 3  # It waited for the timeout, and no longer.
    # The refused request stored nothing; the holder's own transaction never committed.
    assert len(await _positions(committed, account)) == (1 if has_row else 0)


async def test__progress_lock__a_timeout_on_the_guess_insert_gives_service_unavailable(
    committed: Committed, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock on the game is free, but a holder keeps `FOR UPDATE` on the guessed title's row, and
    # inserting the guess (a foreign key, so `FOR KEY SHARE` on that row) must wait for it.
    monkeypatch.setattr(progress_crud, "LOCK_TIMEOUT_MS", 300)
    account = await committed.new_user()
    await committed.guess(account, SKIP)
    async with committed.sessions() as holder:
        await holder.execute(
            select(Title.id).where(Title.id == committed.ids["other"]).with_for_update()
        )

        with pytest.raises(ServiceUnavailableError) as error:
            await committed.guess(account, "other")

    assert (error.value.status_code, error.value.code) == (503, "service_unavailable")
    assert await _positions(committed, account) == [
        (1, None)
    ]  # Nothing was stored; a retry is safe.
    retry = await committed.guess(account, "other")
    assert retry.guesses_used == 2
