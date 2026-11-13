"""The hint of each step and the source link, made from one IGDB game (SPEC-04 AC-30 to AC-33).

Pure: data in, data out. A hint whose data is missing is `None`, never invented. Values are English,
as IGDB gives them, at most three names joined with ", ", at most 255 characters.
"""

import math
import re
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import NamedTuple

from app.content.igdb import IgdbGame

# `puzzle_clues.hint_value` is 255 characters.
MAX_HINT_LENGTH = 255
MAX_HINT_NAMES = 3
# AC-31: a critic score rests on at least this many critic scores, or there is no hint.
MIN_CRITIC_SCORES = 4
IGDB_GAME_URL = "https://www.igdb.com/games/"
# `fullmatch`, not `$`, which also accepts a trailing newline.
IGDB_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)


class HintKind(StrEnum):
    """The `puzzle_clues.hint_kind` values (CONTRACTS.md). `metacritic_score` is never produced."""

    CRITIC_SCORE = "critic_score"
    PLATFORM = "platform"
    GENRE = "genre"
    YEAR = "year"
    DEVELOPER = "developer"


class Hint(NamedTuple):
    kind: HintKind
    value: str


def critic_score(game: IgdbGame) -> str | None:
    """IGDB's aggregated critic score as a whole number, rounded half up (R-19: Python's `round`
    turns 84.5 into 84). `None` unless at least `MIN_CRITIC_SCORES` scores back it."""
    rating = game.aggregated_rating
    if rating is None or not math.isfinite(rating):
        return None
    if (game.aggregated_rating_count or 0) < MIN_CRITIC_SCORES:
        return None
    return str(Decimal(str(rating)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def join_names(names: Iterable[str]) -> str | None:
    """At most three different names, in order, joined with ", ". When the text is too long, whole
    names are left out from the end, never cut mid-word; `None` when no name fits."""
    kept: list[str] = []
    for name in (name.strip() for name in names):
        if name and name not in kept:
            kept.append(name)
    kept = kept[:MAX_HINT_NAMES]
    while kept and len(", ".join(kept)) > MAX_HINT_LENGTH:
        kept.pop()
    return ", ".join(kept) or None


def hints(game: IgdbGame) -> dict[int, Hint | None]:
    """Steps 1 to 6 (AC-30): none, critic score, platform of the first release, genre, release
    year, developer."""
    year = game.release_year
    values = {
        2: (HintKind.CRITIC_SCORE, critic_score(game)),
        3: (HintKind.PLATFORM, join_names(game.first_release_platforms)),
        4: (HintKind.GENRE, join_names(genre.name for genre in game.genres)),
        5: (HintKind.YEAR, None if year is None else str(year)),
        6: (HintKind.DEVELOPER, join_names(game.developers)),
    }
    result: dict[int, Hint | None] = {1: None}
    for step, (kind, value) in values.items():
        result[step] = None if value is None else Hint(kind, value)
    return result


def is_valid_slug(slug: str) -> bool:
    return IGDB_SLUG.fullmatch(slug) is not None


def source_url(game: IgdbGame) -> str:
    """The game's page on igdb.com, built here and never copied from IGDB (R-18, AC-33). The slug
    must be a plain IGDB slug, else `ValueError`."""
    if not is_valid_slug(game.slug):
        raise ValueError("the game's IGDB slug is not valid")
    return f"{IGDB_GAME_URL}{game.slug}"
