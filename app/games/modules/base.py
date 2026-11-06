"""The contract between the site and a game module (AC-2).

The module does no database or network I/O (R-6), so a guest's replay and an account's stored
state call the same code (AC-44).
"""

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.games.enums import GuessResult, ProgressStatus
from app.games.schemas import PuzzleView


class GuessError(Exception):
    """The module knows nothing of HTTP; the service maps these to the API's codes."""


class TitleAlreadyGuessedError(GuessError):
    pass


class TitleKindMismatchError(GuessError):
    pass


class GameFinishedError(GuessError):
    pass


@dataclass(frozen=True)
class TitleData:
    id: int
    kind: str
    name: str
    normalized_name: str
    franchise: str | None
    normalized_franchise: str | None
    year: int | None


@dataclass(frozen=True)
class MediaFileData:
    """The module sees the key, never a path or a bucket."""

    role: str
    key: str
    content_type: str
    width: int
    height: int


@dataclass(frozen=True)
class ClueData:
    step: int
    hint_kind: str | None
    hint_value: str | None
    source_url: str | None
    media_type: str
    media_files: tuple[MediaFileData, ...]


@dataclass(frozen=True)
class SignedLink:
    url: str
    expires_at: dt.datetime


# The module calls it only for the steps it shows, so an unearned step never gets a link (AC-71).
MediaSigner = Callable[[str, str], SignedLink]


@dataclass(frozen=True)
class PuzzleData:
    slug: str
    variant: str
    title_kind: str
    date: dt.date
    number: int
    answer: TitleData
    clues: tuple[ClueData, ...]


@dataclass(frozen=True)
class GuessEntry:
    """`title` is None for a skip."""

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
    max_guesses: int

    def has_variant(self, variant: str, *, default_variant: str) -> bool: ...

    def get_puzzle(
        self, puzzle: PuzzleData, state: ProgressState, *, is_signed_in: bool, sign: MediaSigner
    ) -> PuzzleView:
        """Never the answer or an unearned clue before the game ends (AC-4); `sign` is called for
        the shown steps only (AC-71)."""
        ...

    def submit_guess(
        self, puzzle: PuzzleData, state: ProgressState, guess: TitleData | None
    ) -> tuple[ProgressState, GuessResult]:
        """`None` is a skip. Raises `GameFinishedError`, `TitleKindMismatchError` or
        `TitleAlreadyGuessedError`, checked in that order, and changes nothing."""
        ...
