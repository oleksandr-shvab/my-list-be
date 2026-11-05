"""The game guesser: six clues, six guesses, a title to find.

Clue steps: 1 is a pixelated crop with no hint; 2 to 5 are screenshots with the hints
`metacritic_score`, `platform`, `genre` and `year` (a title with no score has a null hint); 6 is
a muted looping clip with the hint `developer`. `hint_kind` stays free text in the database,
because other games will have other hints.
"""

from dataclasses import replace

from app.core.exceptions import PuzzleDataError
from app.games.enums import GuessResult, ProgressStatus
from app.games.modules.base import (
    ClueData,
    GameFinishedError,
    GuessEntry,
    ProgressState,
    PuzzleData,
    TitleAlreadyGuessedError,
    TitleData,
    TitleKindMismatchError,
)
from app.games.schemas import AnswerRead, GameGuesserView, GuessRead, HintRead, StepRead

MAX_GUESSES = 6
STEP_COUNT = 6


class GameGuesserModule:
    max_guesses = MAX_GUESSES

    def has_variant(self, variant: str, *, default_variant: str) -> bool:
        # The game has one variant for now: its default (R-4). A list arrives with the word game.
        return variant == default_variant

    def get_puzzle(
        self, puzzle: PuzzleData, state: ProgressState, *, is_signed_in: bool
    ) -> GameGuesserView:
        steps = [clue.step for clue in puzzle.clues]
        if steps != list(range(1, STEP_COUNT + 1)):
            raise PuzzleDataError(f"Puzzle {puzzle.slug} {puzzle.date} has clue steps {steps}")

        guesses_used = len(state.guesses)
        is_finished = state.status != ProgressStatus.IN_PROGRESS
        earned_steps = STEP_COUNT if is_finished else guesses_used + 1
        answer = puzzle.answer
        return GameGuesserView(
            type="game_guesser",
            slug=puzzle.slug,
            variant=puzzle.variant,
            date=puzzle.date,
            number=puzzle.number,
            status=state.status,
            max_guesses=MAX_GUESSES,
            guesses_used=guesses_used,
            guesses_left=MAX_GUESSES - guesses_used,
            guesses=[
                GuessRead(
                    title_id=entry.title.id if entry.title else None,
                    name=entry.title.name if entry.title else None,
                    result=entry.result,
                )
                for entry in state.guesses
            ],
            steps=[_step_view(clue, is_finished) for clue in puzzle.clues[:earned_steps]],
            answer=_answer_view(answer) if is_finished else None,
            counts_for_streak=state.counts_for_streak,
            signed_in=is_signed_in,
        )

    def submit_guess(
        self, puzzle: PuzzleData, state: ProgressState, guess: TitleData | None
    ) -> tuple[ProgressState, GuessResult]:
        """A skip (`None`) is `skipped`. A guess is correct when it is the answer title or has the
        same normalized name (Q9), `franchise` when it is wrong and both normalized franchises are
        present and equal (a missing franchise never matches, AC-28), else `wrong`. A correct
        guess wins; a sixth guess or skip that is not correct loses (AC-29). The score is the
        number of guesses used, skips included (AC-30)."""
        if state.status != ProgressStatus.IN_PROGRESS:
            raise GameFinishedError()
        if guess is not None:
            if guess.kind != puzzle.title_kind:
                raise TitleKindMismatchError()
            if any(_is_same_title(entry.title, guess) for entry in state.guesses):
                raise TitleAlreadyGuessedError()

        result = _grade(puzzle.answer, guess)
        guesses = (*state.guesses, GuessEntry(title=guess, result=result))
        if result == GuessResult.CORRECT:
            status = ProgressStatus.WON
        elif len(guesses) >= MAX_GUESSES:
            status = ProgressStatus.LOST
        else:
            status = ProgressStatus.IN_PROGRESS
        return replace(state, status=status, guesses=guesses), result


def _is_same_title(known: TitleData | None, guess: TitleData) -> bool:
    """The equivalence of Q9, used for both the answer and a repeated guess: the same title or the
    same normalized name ("Portal 2" and "PORTAL  2" are one guess)."""
    return known is not None and (
        known.id == guess.id or known.normalized_name == guess.normalized_name
    )


def _grade(answer: TitleData, guess: TitleData | None) -> GuessResult:
    if guess is None:
        return GuessResult.SKIPPED
    if _is_same_title(answer, guess):
        return GuessResult.CORRECT
    # Truthy, not just present: an empty franchise is a missing one and never matches (AC-28).
    if guess.normalized_franchise and guess.normalized_franchise == answer.normalized_franchise:
        return GuessResult.FRANCHISE
    return GuessResult.WRONG


def _step_view(clue: ClueData, is_finished: bool) -> StepRead:
    hint = (
        HintRead(kind=clue.hint_kind, value=clue.hint_value)
        if clue.hint_kind is not None and clue.hint_value is not None
        else None
    )
    return StepRead(step=clue.step, hint=hint, source_url=clue.source_url if is_finished else None)


def _answer_view(answer: TitleData) -> AnswerRead:
    return AnswerRead(
        title_id=answer.id, name=answer.name, franchise=answer.franchise, year=answer.year
    )
