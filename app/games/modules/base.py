"""The contract between the shared site and a game module (AC-2).

The site owns routing, visibility, progress storage, stats, the archive and the clock. A module
owns what a puzzle shows, what a guess is and how the game ends. The two sides exchange plain
data (the dataclasses below) and the module does no database or network I/O (R-6), so a guest's
replay and an account's stored state call the same code (AC-44).
"""

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from app.games.enums import GuessResult, ProgressStatus
from app.games.schemas import PuzzleView


@dataclass(frozen=True)
class TitleData:
    id: int
    name: str
    normalized_name: str
    franchise: str | None
    normalized_franchise: str | None
    year: int | None


@dataclass(frozen=True)
class ClueData:
    step: int
    hint_kind: str | None
    hint_value: str | None
    source_url: str | None


@dataclass(frozen=True)
class PuzzleData:
    slug: str
    variant: str
    date: dt.date
    number: int
    answer: TitleData
    clues: tuple[ClueData, ...]


@dataclass(frozen=True)
class GuessEntry:
    """One stored guess. `title` is None for a skip."""

    title: TitleData | None
    result: GuessResult


@dataclass(frozen=True)
class ProgressState:
    status: ProgressStatus = ProgressStatus.IN_PROGRESS
    guesses: tuple[GuessEntry, ...] = ()
    # None while nothing says whether the play counts for the streak (a guest after its first
    # request, or an account with no stored row).
    counts_for_streak: bool | None = None


class GameModule(Protocol):
    def has_variant(self, variant: str, *, default_variant: str) -> bool:
        """Whether the game has this variant."""
        ...

    def get_puzzle(
        self, puzzle: PuzzleData, state: ProgressState, *, is_signed_in: bool
    ) -> PuzzleView:
        """The view the player may see now: never the answer or a clue not yet earned before the
        game ends (AC-4)."""
        ...

    def submit_guess(
        self, puzzle: PuzzleData, state: ProgressState, guess: TitleData | None
    ) -> tuple[ProgressState, GuessResult]:
        """Apply a guess (`None` is a skip) and return the new state and the feedback."""
        ...
