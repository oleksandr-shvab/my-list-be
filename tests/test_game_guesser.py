"""The guess rules of the game guesser, on plain data (no database): AC-2, AC-27 to AC-33."""

import datetime as dt
from dataclasses import replace

import pytest

from app.games.enums import GuessResult, ProgressStatus
from app.games.modules.base import (
    GameFinishedError,
    ProgressState,
    PuzzleData,
    TitleAlreadyGuessedError,
    TitleData,
    TitleKindMismatchError,
)
from app.games.modules.game_guesser import GameGuesserModule
from app.games.normalize import normalize_text
from tests.factories import make_clue


def _title(
    title_id: int, name: str, franchise: str | None = None, kind: str = "video_game"
) -> TitleData:
    return TitleData(
        id=title_id,
        kind=kind,
        name=name,
        normalized_name=normalize_text(name),
        franchise=franchise,
        normalized_franchise=normalize_text(franchise) if franchise else None,
        year=2004,
    )


# Key to title. The answer is "Half-Life 2" of the franchise "Half-Life".
TITLES = {
    "answer": _title(1, "Half-Life 2", "Half-Life"),
    "same_name": _title(2, "HALF LIFE 2", None),  # A near-duplicate row: same normalized name.
    "sibling": _title(3, "Half-Life: Alyx", "half life"),  # Same normalized franchise.
    "other": _title(4, "Portal 2", "Portal"),
    "other_2": _title(5, "Quake", "Quake"),
    "other_3": _title(6, "Doom", None),
    "no_franchise": _title(7, "Mario", None),
    "other_again": _title(10, "PORTAL  2", None),  # Same normalized name as "other" (Q9).
    "board_game": _title(8, "Catan", None, kind="board_game"),
}
# The same puzzle with an answer that has no franchise.
ANSWER_WITHOUT_FRANCHISE = _title(9, "Pong", None)
# A franchise that normalizes to nothing is stored as '' by a careless writer; it still must not
# match (the crud layer stores NULL, so this guards the module on its own).
ANSWER_WITH_EMPTY_FRANCHISE = replace(
    _title(11, "Tetris", ""), franchise="", normalized_franchise=""
)
TITLES["empty_franchise"] = replace(
    _title(12, "Dr. Mario", ""), franchise="", normalized_franchise=""
)


def _puzzle(answer: TitleData = TITLES["answer"]) -> PuzzleData:
    return PuzzleData(
        slug="game-guesser",
        variant="default",
        title_kind="video_game",
        date=dt.date(2026, 11, 3),
        number=4,
        answer=answer,
        clues=tuple(make_clue(step) for step in range(1, 7)),
    )


def _title_of(key: str | None) -> TitleData | None:
    return None if key is None else TITLES[key]


def _play(
    keys: list[str | None], *, puzzle: PuzzleData | None = None, state: ProgressState | None = None
) -> tuple[ProgressState, GuessResult]:
    """Apply the guesses (a key, or None for a skip) in order; return the final state and the
    last feedback."""
    module = GameGuesserModule()
    puzzle = puzzle or _puzzle()
    state = state or ProgressState()
    result = GuessResult.SKIPPED
    for key in keys:
        state, result = module.submit_guess(puzzle, state, _title_of(key))
    return state, result


WON = ProgressStatus.WON
LOST = ProgressStatus.LOST
RUNNING = ProgressStatus.IN_PROGRESS
WITH_NO_FRANCHISE_ANSWER = _puzzle(ANSWER_WITHOUT_FRANCHISE)
WITH_EMPTY_FRANCHISE_ANSWER = _puzzle(ANSWER_WITH_EMPTY_FRANCHISE)


@pytest.mark.parametrize(
    ("keys", "puzzle", "result", "status"),
    [
        # Correct: the answer, or a title with the same normalized name (Q9, AC-27).
        pytest.param(["answer"], None, GuessResult.CORRECT, WON, id="answer"),
        pytest.param(["same_name"], None, GuessResult.CORRECT, WON, id="near_duplicate_title"),
        # Franchise: wrong, same normalized franchise (AC-28).
        pytest.param(["sibling"], None, GuessResult.FRANCHISE, RUNNING, id="same_franchise"),
        pytest.param(["other"], None, GuessResult.WRONG, RUNNING, id="other_franchise"),
        pytest.param(["no_franchise"], None, GuessResult.WRONG, RUNNING, id="guess_no_franchise"),
        pytest.param(
            ["other_3"],
            WITH_NO_FRANCHISE_ANSWER,
            GuessResult.WRONG,
            RUNNING,
            id="both_no_franchise",
        ),
        pytest.param(
            ["sibling"],
            WITH_NO_FRANCHISE_ANSWER,
            GuessResult.WRONG,
            RUNNING,
            id="answer_no_franchise",
        ),
        pytest.param(
            ["empty_franchise"],
            WITH_EMPTY_FRANCHISE_ANSWER,
            GuessResult.WRONG,
            RUNNING,
            id="both_empty_franchise",
        ),
        # A skip is never correct and never gets the flag (AC-28).
        pytest.param([None], None, GuessResult.SKIPPED, RUNNING, id="skip"),
        # The sixth guess or skip that is not correct loses; a correct one still wins (AC-29).
        pytest.param([None] * 5 + ["other"], None, GuessResult.WRONG, LOST, id="sixth_wrong"),
        pytest.param([None] * 6, None, GuessResult.SKIPPED, LOST, id="sixth_skip"),
        pytest.param(
            [None] * 5 + ["sibling"], None, GuessResult.FRANCHISE, LOST, id="sixth_franchise"
        ),
        pytest.param([None] * 5 + ["answer"], None, GuessResult.CORRECT, WON, id="sixth_correct"),
        pytest.param([None] * 4 + ["other"], None, GuessResult.WRONG, RUNNING, id="fifth_wrong"),
        # Two skips are not duplicates (AC-31 is about titles).
        pytest.param([None, None, None], None, GuessResult.SKIPPED, RUNNING, id="skips_repeat"),
    ],
)
def test__submit_guess__result_and_status(
    keys: list[str | None], puzzle: PuzzleData | None, result: GuessResult, status: ProgressStatus
) -> None:
    state, last_result = _play(keys, puzzle=puzzle)

    assert last_result == result
    assert state.status == status
    # AC-30: the score is the number of guesses used, skips included.
    assert len(state.guesses) == len(keys)
    assert state.guesses[-1].result == result
    assert state.guesses[-1].title == _title_of(keys[-1])


def test__submit_guess__score_counts_skips_and_the_input_state_is_untouched() -> None:
    before = ProgressState(counts_for_streak=True)

    state, _ = _play([None, "other", "answer"], state=before)

    assert (state.status, len(state.guesses)) == (WON, 3)
    assert state.counts_for_streak is True
    assert before == ProgressState(counts_for_streak=True)
    assert before.guesses == ()


@pytest.mark.parametrize(
    ("keys", "error"),
    [
        pytest.param(["other", "other"], TitleAlreadyGuessedError, id="duplicate"),
        pytest.param(["other", None, "other"], TitleAlreadyGuessedError, id="duplicate_after_skip"),
        pytest.param(["other", "other_again"], TitleAlreadyGuessedError, id="duplicate_by_name"),
        pytest.param(
            ["other_again", None, "other"],
            TitleAlreadyGuessedError,
            id="duplicate_by_name_reversed",
        ),
        pytest.param(
            [None] * 5 + ["other", "same_name"], GameFinishedError, id="correct_by_name_after_lost"
        ),
        pytest.param(["board_game"], TitleKindMismatchError, id="other_title_kind"),
        pytest.param(["answer", None], GameFinishedError, id="after_won"),
        pytest.param(["answer", "other"], GameFinishedError, id="title_after_won"),
        pytest.param([None] * 6 + [None], GameFinishedError, id="after_lost"),
        # The finished check runs first, so a finished game never reports a duplicate.
        pytest.param(["answer", "answer"], GameFinishedError, id="finished_before_duplicate"),
    ],
)
def test__submit_guess__refused_guesses_raise_and_use_no_guess(
    keys: list[str | None], error: type[Exception]
) -> None:
    state, _ = _play(keys[:-1])

    with pytest.raises(error):
        GameGuesserModule().submit_guess(_puzzle(), state, _title_of(keys[-1]))
