"""The game guesser: six clues, six guesses, a title to find.

Clue steps: 1 is a pixelated crop with no hint; 2 to 5 are screenshots with the hints
`metacritic_score`, `platform`, `genre` and `year` (a title with no score has a null hint); 6 is
a muted looping clip with the hint `developer`. `hint_kind` stays free text in the database,
because other games will have other hints.

Media (AC-13b): an image clue has one `image/webp` file and no poster. A video clue has one
`video/mp4` file, at most one `video/webm` file and one poster (an `image/webp` still frame). The
view checks this on every read, for all six steps, so a broken puzzle fails at step 1 and not
when the player reaches step 6.
"""

from dataclasses import dataclass, replace

from app.core.exceptions import PuzzleDataError
from app.games.enums import ClueContentType, ClueRole, GuessResult, MediaType, ProgressStatus
from app.games.modules.base import (
    ClueData,
    GameFinishedError,
    GuessEntry,
    MediaFileData,
    MediaSigner,
    ProgressState,
    PuzzleData,
    TitleAlreadyGuessedError,
    TitleData,
    TitleKindMismatchError,
)
from app.games.schemas import (
    AnswerRead,
    GameGuesserView,
    GuessRead,
    HintRead,
    MediaRead,
    MediaSourceRead,
    PosterRead,
    StepRead,
)

MAX_GUESSES = 6
STEP_COUNT = 6


class GameGuesserModule:
    max_guesses = MAX_GUESSES

    def has_variant(self, variant: str, *, default_variant: str) -> bool:
        # The game has one variant for now: its default (R-4). A list arrives with the word game.
        return variant == default_variant

    def get_puzzle(
        self, puzzle: PuzzleData, state: ProgressState, *, is_signed_in: bool, sign: MediaSigner
    ) -> GameGuesserView:
        steps = [clue.step for clue in puzzle.clues]
        if steps != list(range(1, STEP_COUNT + 1)):
            raise PuzzleDataError(f"Puzzle {puzzle.slug} {puzzle.date} has clue steps {steps}")
        # Every step is checked, the unearned ones too; only the earned ones are signed below.
        layouts = [_media_layout(puzzle, clue) for clue in puzzle.clues]

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
            steps=[
                _step_view(clue, layout, is_finished, sign)
                for clue, layout in zip(
                    puzzle.clues[:earned_steps], layouts[:earned_steps], strict=True
                )
            ],
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


@dataclass(frozen=True)
class _MediaLayout:
    """The files of a clue that passed the AC-13b check: the sources in the order a browser should
    try them (WebM first, MP4 last) and the poster of a video."""

    sources: tuple[MediaFileData, ...]
    poster: MediaFileData | None


def _media_layout(puzzle: PuzzleData, clue: ClueData) -> _MediaLayout:
    sources = [file for file in clue.media_files if file.role == ClueRole.SOURCE]
    posters = [file for file in clue.media_files if file.role == ClueRole.POSTER]
    source_types = sorted(file.content_type for file in sources)
    if clue.media_type == MediaType.VIDEO:
        # "video/mp4" sorts before "video/webm"; WebM goes first in the view, so reverse it.
        is_valid = (
            source_types
            in (
                [ClueContentType.VIDEO_MP4],
                [ClueContentType.VIDEO_MP4, ClueContentType.VIDEO_WEBM],
            )
            and len(posters) == 1
        )
        ordered = sorted(sources, key=lambda file: file.content_type, reverse=True)
    elif clue.media_type == MediaType.IMAGE:
        is_valid = source_types == [ClueContentType.IMAGE_WEBP] and not posters
        ordered = sources
    else:
        is_valid = False
        ordered = sources
    if not is_valid:
        raise PuzzleDataError(
            f"Puzzle {puzzle.slug} {puzzle.date} step {clue.step} has {clue.media_type} media "
            f"files {source_types} and {len(posters)} poster(s), which break AC-13b"
        )
    return _MediaLayout(sources=tuple(ordered), poster=posters[0] if posters else None)


def _step_view(
    clue: ClueData, layout: _MediaLayout, is_finished: bool, sign: MediaSigner
) -> StepRead:
    hint = (
        HintRead(kind=clue.hint_kind, value=clue.hint_value)
        if clue.hint_kind is not None and clue.hint_value is not None
        else None
    )
    return StepRead(
        step=clue.step,
        hint=hint,
        media=_media_view(clue, layout, sign),
        source_url=clue.source_url if is_finished else None,
    )


def _media_view(clue: ClueData, layout: _MediaLayout, sign: MediaSigner) -> MediaRead:
    links = [sign(file.key, file.content_type) for file in layout.sources]
    # The shown size and expiry are those of the last source, the MP4 of a video (always there).
    primary, primary_link = layout.sources[-1], links[-1]
    poster = None
    if layout.poster is not None:
        poster_link = sign(layout.poster.key, layout.poster.content_type)
        poster = PosterRead(
            url=poster_link.url,
            content_type=layout.poster.content_type,
            expires_at=poster_link.expires_at,
        )
    return MediaRead(
        type=MediaType(clue.media_type),
        width=primary.width,
        height=primary.height,
        expires_at=primary_link.expires_at,
        sources=[
            MediaSourceRead(url=link.url, content_type=file.content_type)
            for file, link in zip(layout.sources, links, strict=True)
        ],
        poster=poster,
    )


def _answer_view(answer: TitleData) -> AnswerRead:
    return AnswerRead(
        title_id=answer.id, name=answer.name, franchise=answer.franchise, year=answer.year
    )
