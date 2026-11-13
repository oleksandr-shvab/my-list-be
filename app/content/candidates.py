"""Who may be an answer, and which screenshots a build uses (SPEC-04 AC-21 to AC-25, AC-34, AC-37).

Pure: IGDB games and settings in, choices out. Every choice takes its randomness from a stream of
`app/content/seed.py`, over input sorted first, so it does not depend on the order IGDB answers in.
"""

import datetime as dt
import random
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from app.content.hints import is_valid_slug
from app.content.igdb import GameType, IgdbGame, IgdbScreenshot
from app.content.settings import ContentSettings
from app.crud.exclusion import ExclusionRow
from app.games.enums import ExclusionReason

# AC-21: main games, remakes, remasters and expanded games. A subset of the types the title import
# keeps (`titles_import.IN_SCOPE_TYPES`), so every candidate is in the search list (R-20).
CANDIDATE_TYPES = frozenset(
    {GameType.MAIN_GAME, GameType.REMAKE, GameType.REMASTER, GameType.EXPANDED_GAME}
)
# IGDB's where clause for a run that reads candidates; `funnel` applies the same rules again.
IGDB_CANDIDATES_WHERE = (
    f"game_type = ({','.join(str(int(kind)) for kind in sorted(CANDIDATE_TYPES))})"
    " & version_parent = null"
)

STEP_SCREENSHOTS = 4  # steps 2 to 5
CROP_SCREENSHOTS = 1  # step 1
MIN_CLIP_SCREENSHOTS = 2  # step 6
MAX_CLIP_SCREENSHOTS = 3
# The fewest screenshots a build needs (AC-34, AC-37), whatever the setting says.
REQUIRED_SCREENSHOTS = STEP_SCREENSHOTS + CROP_SCREENSHOTS + MIN_CLIP_SCREENSHOTS


class NotBuildableError(Exception):
    """No answer or no screenshot set is left. The message holds no game name."""


class FunnelStep(NamedTuple):
    name: str
    passed: int


@dataclass(frozen=True)
class Funnel:
    """How many games pass each filter, in order, and the games that passed all (AC-24)."""

    steps: tuple[FunnelStep, ...]
    candidates: tuple[IgdbGame, ...]


def eligible_screenshots(game: IgdbGame, settings: ContentSettings) -> list[IgdbScreenshot]:
    """The screenshots big enough for a clue, one for each image id, sorted by image id (AC-21)."""
    unique = {
        shot.image_id: shot
        for shot in game.screenshots
        if shot.width >= settings.CANDIDATE_MIN_WIDTH
        and shot.height >= settings.CANDIDATE_MIN_HEIGHT
    }
    return [unique[image_id] for image_id in sorted(unique)]


def never_igdb_ids(exclusions: Iterable[ExclusionRow]) -> set[int]:
    """The IGDB ids of the titles a reviewer rejected as "never" (AC-23). Rows repeat for one
    title and slot; a `rebuild` row keeps its title a candidate."""
    return {
        row.igdb_id
        for row in exclusions
        if row.reason == ExclusionReason.NEVER and row.igdb_id is not None
    }


def funnel(
    games: Iterable[IgdbGame],
    *,
    today: dt.date,
    settings: ContentSettings,
    earlier_answer_igdb_ids: Collection[int],
    never_igdb_ids: Collection[int],
) -> Funnel:
    """The filters of AC-21 in order, each applied to what the one before kept (AC-24)."""
    remaining = list(games)
    steps = [FunnelStep("games read", len(remaining))]
    latest_release = today - dt.timedelta(days=settings.CANDIDATE_MIN_AGE_DAYS)
    min_screenshots = max(settings.CANDIDATE_MIN_SCREENSHOTS, REQUIRED_SCREENSHOTS)

    def released_in_time(game: IgdbGame) -> bool:
        if game.first_release_date is None:
            return False
        released = dt.datetime.fromtimestamp(game.first_release_date, dt.UTC).date()
        return released <= latest_release

    rules = [
        (
            "game type, not a version of another game",
            lambda game: game.game_type in CANDIDATE_TYPES and game.version_parent is None,
        ),
        (f"released {settings.CANDIDATE_MIN_AGE_DAYS} days ago or earlier", released_in_time),
        (
            f"at least {settings.CANDIDATE_MIN_RATINGS} ratings",
            lambda game: (
                (getattr(game, settings.CANDIDATE_RATING_FIELD) or 0)
                >= settings.CANDIDATE_MIN_RATINGS
            ),
        ),
        (
            (
                f"at least {min_screenshots} screenshots of "
                f"{settings.CANDIDATE_MIN_WIDTH}x{settings.CANDIDATE_MIN_HEIGHT} or more"
            ),
            lambda game: len(eligible_screenshots(game, settings)) >= min_screenshots,
        ),
        ("a valid IGDB slug", lambda game: is_valid_slug(game.slug)),
        ("not rejected as never", lambda game: game.id not in never_igdb_ids),
        ("not an earlier answer", lambda game: game.id not in earlier_answer_igdb_ids),
    ]
    for name, keep in rules:
        remaining = [game for game in remaining if keep(game)]
        steps.append(FunnelStep(name, len(remaining)))
    return Funnel(tuple(steps), tuple(remaining))


def choose_answer(candidates: Sequence[IgdbGame], stream: random.Random) -> IgdbGame:
    """One candidate, drawn from the list sorted by IGDB id (AC-26, AC-28)."""
    if not candidates:
        raise NotBuildableError("no candidate is left")
    return stream.choice(sorted(candidates, key=lambda game: game.id))


@dataclass(frozen=True)
class ScreenshotPlan:
    """Which screenshot goes where. Every one is used once: the step-1 crop is never one of the
    four full screenshots of steps 2 to 5 (AC-34), and the clip uses none of the five (AC-37)."""

    steps: tuple[IgdbScreenshot, ...]  # steps 2 to 5, in step order
    crop: IgdbScreenshot  # step 1
    clip: tuple[IgdbScreenshot, ...]  # step 6, two or three


def assign_screenshots(
    game: IgdbGame,
    stream: random.Random,
    avoid_ids: Collection[str],
    *,
    settings: ContentSettings,
) -> ScreenshotPlan:
    """Pick the screenshots of one build from the eligible ones, minus `avoid_ids` (the ones a
    rebuild must not use again, R-9). Fewer than `REQUIRED_SCREENSHOTS` left is `NotBuildableError`."""
    pool = [shot for shot in eligible_screenshots(game, settings) if shot.image_id not in avoid_ids]
    if len(pool) < REQUIRED_SCREENSHOTS:
        raise NotBuildableError(
            f"{len(pool)} usable screenshots, {REQUIRED_SCREENSHOTS} are needed"
        )
    spare = len(pool) - STEP_SCREENSHOTS - CROP_SCREENSHOTS
    clip_count = min(stream.choice((MIN_CLIP_SCREENSHOTS, MAX_CLIP_SCREENSHOTS)), spare)
    # One draw without replacement, so no screenshot can be in two places.
    picks = stream.sample(pool, STEP_SCREENSHOTS + CROP_SCREENSHOTS + clip_count)
    return ScreenshotPlan(
        steps=tuple(picks[:STEP_SCREENSHOTS]),
        crop=picks[STEP_SCREENSHOTS],
        clip=tuple(picks[STEP_SCREENSHOTS + CROP_SCREENSHOTS :]),
    )
