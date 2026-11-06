"""Guesses and the guest replay (AC-4, AC-26 to AC-34, AC-37, AC-39, AC-40).

The rules live in the game module (`tests/test_game_guesser.py`). Here: the service that replays
a guest's `previous` and applies the new guess, and the route.
"""

import datetime as dt
import re
from dataclasses import replace

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException
from app.crud.title import create_title
from app.games import service
from app.games.schemas import PuzzleView
from app.models.progress import Progress
from app.models.title import Title
from tests.conftest import SetNow
from tests.factories import (
    IMAGE_TTL_SECONDS,
    TEST_MEDIA_BASE_URL,
    VIDEO_TTL_SECONDS,
    create_game,
    create_puzzle,
    make_local_storage,
)

NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.UTC)
TODAY = NOW.date()
LAUNCH = TODAY - dt.timedelta(days=3)
ANSWER_NAME = "Secretname Quest"
ANSWER_FRANCHISE = "Secretfranchise"
GUESS_URL = "/v1/games/game-guesser/puzzles/{date}/guesses"

# Name to title id, plus the tokens the tables use for ids that are not titles.
Ids = dict[str, int]
SKIP = None
UNKNOWN = 987_654
TOO_LARGE_FOR_A_KEY = 2**40

CODE_STATUS = {
    "duplicate_guess": 422,
    "title_not_found": 422,
    "invalid_guess_history": 422,
    "puzzle_already_finished": 409,
    "puzzle_not_found": 404,
    "variant_not_found": 404,
    "game_not_found": 404,
}


@pytest.fixture
async def ids(db_session: AsyncSession, set_now: SetNow) -> Ids:
    """Puzzles for today, yesterday and tomorrow (hidden); the answer is "Secretname Quest"."""
    set_now(NOW)
    game = await create_game(db_session, launch_date=LAUNCH)
    titles = {
        "answer": await create_title(
            db_session, name=ANSWER_NAME, franchise=ANSWER_FRANCHISE, year=1987
        ),
        # Same franchise as the answer, and a name that has none of the answer's words.
        "sibling": await create_title(
            db_session, name="Origins of Secrets", franchise=ANSWER_FRANCHISE
        ),
        "other": await create_title(db_session, name="Portal 2", franchise="Portal"),
        "other_2": await create_title(db_session, name="Quake", franchise="Quake"),
        # A second row with the same normalized name as "other" (Q9).
        # A second row for the answer: same normalized name, another id (Q9).
        "answer_twin": await create_title(db_session, name="SECRETNAME  QUEST"),
        "other_again": await create_title(db_session, name="PORTAL  2"),
    }
    for offset in (0, -1, 1):
        await create_puzzle(
            db_session,
            game=game,
            answer=titles["answer"],
            puzzle_date=TODAY + dt.timedelta(days=offset),
        )
    return {name: title.id for name, title in titles.items()}


def _resolve(ids: Ids, token: str | int | None) -> int | None:
    return ids[token] if isinstance(token, str) else token


async def _guess(
    db: AsyncSession,
    ids: Ids,
    guess: str | int | None,
    previous: list[str | int | None],
    *,
    now: dt.datetime = NOW,
    puzzle_date: dt.date = TODAY,
    slug: str = "game-guesser",
    variant: str | None = None,
) -> PuzzleView:
    return await service.submit_guess(
        db,
        slug=slug,
        variant=variant,
        puzzle_date=puzzle_date,
        now=now,
        user=None,
        storage=make_local_storage(),
        title_id=_resolve(ids, guess),
        previous=[_resolve(ids, entry) for entry in previous],
    )


# Each row: previous, new guess, then the error code, or None for an accepted guess and the
# status the game has afterwards.
GUESS_ROWS = [
    # Accepted.
    pytest.param([], "other", None, "in_progress", id="first_wrong_guess"),
    pytest.param([], SKIP, None, "in_progress", id="first_skip"),
    pytest.param([SKIP, "other"], "answer", None, "won", id="win_on_the_third"),
    pytest.param([SKIP] * 5, SKIP, None, "lost", id="five_previous_then_the_sixth_loses"),
    pytest.param(
        ["other", SKIP, "sibling"], SKIP, None, "in_progress", id="skips_are_not_duplicates"
    ),
    # Q9: a title with the answer's normalized name wins, and ends the game like the answer.
    pytest.param([], "answer_twin", None, "won", id="answer_twin_wins"),
    pytest.param(["answer_twin"], SKIP, "puzzle_already_finished", None, id="previous_twin_won"),
    # AC-31: a title guessed twice, in `previous` or as the new guess.
    pytest.param(["other"], "other", "duplicate_guess", None, id="duplicate_of_a_previous_guess"),
    pytest.param(
        ["other", SKIP, "sibling"], "sibling", "duplicate_guess", None, id="duplicate_after_skip"
    ),
    pytest.param(["other"], "other_again", "duplicate_guess", None, id="duplicate_by_name"),
    pytest.param(
        [SKIP, "other_again"], "other", "duplicate_guess", None, id="duplicate_by_name_reversed"
    ),
    # AC-32: a title that is not in the game's kind or does not exist.
    pytest.param([], UNKNOWN, "title_not_found", None, id="unknown_title"),
    pytest.param([], 0, "title_not_found", None, id="title_zero"),
    pytest.param([], -5, "title_not_found", None, id="negative_title"),
    pytest.param([], TOO_LARGE_FOR_A_KEY, "title_not_found", None, id="title_beyond_a_key"),
    # AC-33 and AC-39: `previous` already ends the game.
    pytest.param(["answer"], "other", "puzzle_already_finished", None, id="previous_won"),
    pytest.param(["answer"], SKIP, "puzzle_already_finished", None, id="previous_won_then_skip"),
    pytest.param(
        ["answer"], UNKNOWN, "puzzle_already_finished", None, id="finished_before_unknown"
    ),
    # The finished check comes before the duplicate rule: the winning title again is a 409.
    pytest.param(["answer"], "answer", "puzzle_already_finished", None, id="finished_before_dup"),
    # AC-39: a history a real client never sends.
    pytest.param([SKIP] * 6, SKIP, "invalid_guess_history", None, id="six_previous"),
    # The history check comes before the title and duplicate rules of the new guess.
    pytest.param(
        [SKIP] * 6, UNKNOWN, "invalid_guess_history", None, id="history_before_unknown_title"
    ),
    pytest.param(
        ["other"] + [SKIP] * 5, "other", "invalid_guess_history", None, id="history_before_dup"
    ),
    pytest.param(
        [UNKNOWN], UNKNOWN, "invalid_guess_history", None, id="previous_unknown_before_new_unknown"
    ),
    pytest.param(
        [SKIP] * 5 + ["answer"], SKIP, "invalid_guess_history", None, id="six_previous_ending_won"
    ),
    pytest.param([UNKNOWN], "other", "invalid_guess_history", None, id="previous_unknown_title"),
    pytest.param(
        [TOO_LARGE_FOR_A_KEY], "other", "invalid_guess_history", None, id="previous_beyond_a_key"
    ),
    pytest.param(
        ["other", "other"], "sibling", "invalid_guess_history", None, id="previous_duplicate"
    ),
    pytest.param(
        ["other", "other_again"],
        "sibling",
        "invalid_guess_history",
        None,
        id="previous_duplicate_by_name",
    ),
    pytest.param(
        ["answer", "other"], "sibling", "invalid_guess_history", None, id="previous_entry_after_win"
    ),
    pytest.param(
        ["answer", SKIP], "sibling", "invalid_guess_history", None, id="previous_skip_after_win"
    ),
]


@pytest.mark.parametrize(("previous", "guess", "code", "status"), GUESS_ROWS)
async def test__guest_guess__replay_and_new_guess_rules(
    db_session: AsyncSession,
    ids: Ids,
    previous: list[str | int | None],
    guess: str | int | None,
    code: str | None,
    status: str | None,
) -> None:
    if code is not None:
        with pytest.raises(AppException) as error:
            await _guess(db_session, ids, guess, previous)
        assert error.value.code == code
        assert error.value.status_code == CODE_STATUS[code]
        return

    view = await _guess(db_session, ids, guess, previous)

    assert view.status == status
    assert view.guesses_used == len(previous) + 1  # AC-30: skips count.
    assert [entry.title_id for entry in view.guesses] == [
        _resolve(ids, entry) for entry in [*previous, guess]
    ]
    names = {
        title_id: name for title_id, name in await db_session.execute(select(Title.id, Title.name))
    }
    assert [entry.name for entry in view.guesses] == [
        None if entry.title_id is None else names[entry.title_id] for entry in view.guesses
    ]
    # AC-37: nothing is stored, and a guest never gets a progress row.
    assert not db_session.new
    assert not db_session.dirty
    assert await db_session.scalar(select(func.count()).select_from(Progress)) == 0


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        pytest.param(
            {"puzzle_date": TODAY + dt.timedelta(days=1)}, "puzzle_not_found", id="future"
        ),
        pytest.param({"puzzle_date": TODAY - dt.timedelta(days=30)}, "puzzle_not_found", id="none"),
        pytest.param({"variant": "nope"}, "variant_not_found", id="variant"),
        pytest.param({"slug": "no-such-game"}, "game_not_found", id="game"),
    ],
)
async def test__guest_guess__hidden_puzzle_variant_or_game_is_refused_first(
    db_session: AsyncSession, ids: Ids, kwargs: dict[str, object], code: str
) -> None:
    # The guess is valid, so only the puzzle lookup can refuse it (AC-12, AC-17).
    with pytest.raises(AppException) as error:
        await _guess(db_session, ids, "other", [], **kwargs)  # type: ignore[arg-type]

    assert error.value.code == code


@pytest.mark.parametrize(
    ("now", "puzzle_offset", "previous", "expected"),
    [
        # AC-40: a first request on the puzzle's UTC date counts; a day later it does not.
        pytest.param(
            dt.datetime(2026, 11, 3, 23, 59, 59, tzinfo=dt.UTC), 0, [], True, id="last_second"
        ),
        pytest.param(
            dt.datetime(2026, 11, 4, 0, 0, 0, tzinfo=dt.UTC), -1, [], False, id="midnight"
        ),
        pytest.param(
            dt.datetime(2026, 11, 3, 0, 0, 0, tzinfo=dt.UTC), 0, [], True, id="first_second"
        ),
        # Later requests carry null: the browser keeps the first value.
        pytest.param(NOW, 0, [SKIP], None, id="later_request"),
        pytest.param(
            dt.datetime(2026, 11, 4, 0, 0, 0, tzinfo=dt.UTC),
            -1,
            ["other"],
            None,
            id="later_archive",
        ),
    ],
)
async def test__guest_guess__counts_for_streak_only_on_the_first_request(
    db_session: AsyncSession,
    ids: Ids,
    now: dt.datetime,
    puzzle_offset: int,
    previous: list[str | int | None],
    expected: bool | None,
) -> None:
    view = await _guess(
        db_session,
        ids,
        "sibling",
        previous,
        now=now,
        puzzle_date=TODAY + dt.timedelta(days=puzzle_offset),
    )

    assert view.counts_for_streak is expected


async def test__guest_guess__title_of_another_kind_is_title_not_found(
    db_session: AsyncSession, ids: Ids, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No second title kind exists yet, so make every loaded title look foreign. The SQL filter
    # normally stops it first; the module's own check must not become a 500.
    real_title_data = service._title_data
    monkeypatch.setattr(
        service, "_title_data", lambda title: replace(real_title_data(title), kind="board_game")
    )

    with pytest.raises(AppException) as error:
        await _guess(db_session, ids, "other", [])
    assert (error.value.code, error.value.status_code) == ("title_not_found", 422)
    with pytest.raises(AppException) as error:
        await _guess(db_session, ids, SKIP, ["other"])
    assert error.value.code == "invalid_guess_history"


def _leaks(text: str, ids: Ids) -> list[str]:
    """What of the answer shows in a response body: its name, its franchise, its year, or its
    title id (the `title_id` key only a guess or the answer has)."""
    found = [
        word
        for word in (ANSWER_NAME, ANSWER_FRANCHISE)
        if re.search(re.escape(word), text, re.IGNORECASE)
    ]
    if re.search(rf'"title_id":\s*{ids["answer"]}\b', text):
        found.append("answer title_id")
    if re.search(r'"year":\s*1987\b', text):
        found.append("answer year")
    return found


# A won game and a lost game, played through the route with a growing `previous`.
WON_GAME = (
    [SKIP, "other", "answer"],
    ["skipped", "wrong", "correct"],
    "won",
)
LOST_GAME = (
    [SKIP, "other", "sibling", SKIP, "other_2", SKIP],
    ["skipped", "wrong", "franchise", "skipped", "wrong", "skipped"],
    "lost",
)


@pytest.mark.parametrize(
    ("sequence", "results", "final_status"),
    [pytest.param(*WON_GAME, id="won"), pytest.param(*LOST_GAME, id="lost")],
)
async def test__submit_guess__full_game_leaks_nothing_before_the_end(
    client: AsyncClient,
    ids: Ids,
    sequence: list[str | None],
    results: list[str],
    final_status: str,
) -> None:
    previous: list[int | None] = []
    for position, token in enumerate(sequence, start=1):
        response = await client.post(
            GUESS_URL.format(date=TODAY),
            json={"title_id": _resolve(ids, token), "previous": previous},
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        body = response.json()
        assert body["type"] == "game_guesser"
        assert body["signed_in"] is False
        assert [guess["result"] for guess in body["guesses"]] == results[:position]
        assert (body["guesses_used"], body["guesses_left"]) == (position, 6 - position)
        is_last = position == len(sequence)
        if not is_last:
            # AC-26 and AC-29: exactly the earned steps, nothing of the answer.
            assert body["status"] == "in_progress"
            assert [step["step"] for step in body["steps"]] == list(range(1, position + 2))
            assert all(step["source_url"] is None for step in body["steps"])
            assert body["answer"] is None
            assert _leaks(response.text, ids) == []
            assert "example.com" not in response.text
        else:
            assert body["status"] == final_status
            assert [step["step"] for step in body["steps"]] == [1, 2, 3, 4, 5, 6]
            assert all(step["source_url"] for step in body["steps"])
            assert body["answer"] == {
                "title_id": ids["answer"],
                "name": ANSWER_NAME,
                "franchise": ANSWER_FRANCHISE,
                "year": 1987,
            }
        # AC-25: the guess answer carries the signed media of every shown step, as the view does.
        for step in body["steps"]:
            media = step["media"]
            is_video = media["type"] == "video"
            lifetime = VIDEO_TTL_SECONDS if is_video else IMAGE_TTL_SECONDS
            assert media["expires_at"] == _expiry(lifetime)
            assert all(
                source["url"].startswith(f"{TEST_MEDIA_BASE_URL}/v1/media/")
                for source in media["sources"]
            )
            assert (media["poster"] is not None) is is_video
            if is_video:
                assert media["poster"]["url"].startswith(f"{TEST_MEDIA_BASE_URL}/v1/media/")
                assert media["poster"]["expires_at"] == _expiry(IMAGE_TTL_SECONDS)
        previous.append(_resolve(ids, token))


def _expiry(lifetime_seconds: int) -> str:
    return (NOW + dt.timedelta(seconds=lifetime_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(field: str, code: str, **params: int) -> dict[str, object]:
    return {"field": field, "code": code, "params": params}


@pytest.mark.parametrize(
    ("path_date", "body", "error"),
    [
        pytest.param(
            str(TODAY), {"previous": []}, _error("title_id", "required"), id="no_title_id"
        ),
        pytest.param(
            str(TODAY), {"title_id": None}, _error("previous", "required"), id="no_previous"
        ),
        pytest.param(
            str(TODAY),
            {"title_id": "5", "previous": []},
            _error("title_id", "invalid_type"),
            id="string_id",
        ),
        pytest.param(
            str(TODAY),
            {"title_id": True, "previous": []},
            _error("title_id", "invalid_type"),
            id="bool_id",
        ),
        pytest.param(
            str(TODAY),
            {"title_id": None, "previous": 5},
            _error("previous", "invalid_type"),
            id="previous_not_a_list",
        ),
        pytest.param(
            str(TODAY),
            {"title_id": None, "previous": ["a"]},
            _error("previous.0", "invalid_type"),
            id="string_entry",
        ),
        pytest.param(
            str(TODAY),
            {"title_id": None, "previous": [None] * 21},
            _error("previous", "too_long", max=20),
            id="over_the_cap",
        ),
        pytest.param(str(TODAY), [], _error("", "invalid_type"), id="body_not_an_object"),
        pytest.param(
            "2026-02-30",
            {"title_id": None, "previous": []},
            _error("date", "invalid_value"),
            id="bad_date",
        ),
    ],
)
async def test__submit_guess__bad_request_is_a_validation_error(
    client: AsyncClient, path_date: str, body: object, error: dict[str, object]
) -> None:
    response = await client.post(GUESS_URL.format(date=path_date), json=body)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["errors"] == [error]
