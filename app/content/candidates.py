"""Who may be an answer, and which screenshots a build uses (SPEC-04 AC-21 to AC-25, AC-34, AC-37).

Pure: IGDB games and settings in, choices out. Every choice takes its randomness from a stream of
`app/content/seed.py`, over input sorted first, so it does not depend on the order IGDB answers in.
"""

import datetime as dt
import random
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from app.content.hints import is_valid_slug
from app.content.igdb import GameType, IgdbGame, IgdbScreenshot
from app.content.seed import draw_distinct, draw_index
from app.content.settings import ContentSettings
from app.crud.exclusion import ExclusionRow
from app.games.enums import ExclusionReason

# AC-21: main games, remakes, remasters and expanded games. A subset of the types the title import
# keeps (`titles_import.IN_SCOPE_TYPES`), so every candidate is in the search list (R-20).
CANDIDATE_TYPES = frozenset(
    {GameType.MAIN_GAME, GameType.REMAKE, GameType.REMASTER, GameType.EXPANDED_GAME}
)


def candidates_where(settings: ContentSettings) -> str:
    """IGDB's where clause for a run that reads candidates: the candidate types, no versions and
    at least one rating (the floor the title import uses too, so unrated games are never read).
    `funnel` applies the real rules again."""
    types = ",".join(str(int(kind)) for kind in sorted(CANDIDATE_TYPES))
    return f"game_type = ({types}) & version_parent = null & {settings.CANDIDATE_RATING_FIELD} >= 1"


STEP_SCREENSHOTS = 4  # steps 2 to 5
CROP_SCREENSHOTS = 1  # step 1
MIN_CLIP_SCREENSHOTS = 2  # step 6
MAX_CLIP_SCREENSHOTS = 3
# The fewest screenshots a build needs (AC-34, AC-37), whatever the setting says.
REQUIRED_SCREENSHOTS = STEP_SCREENSHOTS + CROP_SCREENSHOTS + MIN_CLIP_SCREENSHOTS
# A rebuild leaves out every screenshot of every rejected draft of its slot (owner decision 71), so
# a game that survives one rebuild has 7 usable screenshots for the draft and 7 more for the rebuild.
REBUILD_SCREENSHOTS = 2 * REQUIRED_SCREENSHOTS


class NotBuildableReason(StrEnum):
    """Why a slot cannot be built; stable, so the builder can act on it."""

    NO_CANDIDATE = "no_candidate"
    TOO_FEW_SCREENSHOTS = "too_few_screenshots"
    CANNOT_REBUILD = "cannot_rebuild"
    BAD_REBUILD_ROW = "bad_rebuild_row"


class NotBuildableError(Exception):
    """No answer or no screenshot set is left for a slot. The message holds no game name."""

    def __init__(self, reason: NotBuildableReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class FunnelStep(NamedTuple):
    name: str
    passed: int


@dataclass(frozen=True)
class Funnel:
    """How many games pass each filter, in order, and the games that passed all (AC-24).
    `survive_one_rebuild` counts the candidates with enough screenshots for a rebuild that leaves
    the five stills out (it is not a filter: a candidate stays a candidate)."""

    steps: tuple[FunnelStep, ...]
    candidates: tuple[IgdbGame, ...]
    survive_one_rebuild: int = 0


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
    title and slot; a `rebuild` row keeps its title a candidate for its own slot only."""
    return {
        row.igdb_id
        for row in exclusions
        if row.reason == ExclusionReason.NEVER and row.igdb_id is not None
    }


def rebuild_reserved_igdb_ids(
    exclusions: Iterable[ExclusionRow], *, own_slot: tuple[str, int] | None = None
) -> set[int]:
    """The IGDB ids of titles a `rebuild` row has reserved for a slot that is not built yet.
    The draft was deleted, so the title is neither an earlier answer nor "never", and without this
    another slot could draw it before the rebuild, and the answer would repeat (AC-25). The slot a
    row reserves it for (`own_slot`, a variant and a slot) may still use it; it is forced there
    and never drawn."""
    return {
        row.igdb_id
        for row in exclusions
        if row.reason == ExclusionReason.REBUILD
        and row.igdb_id is not None
        and (row.variant, row.build_slot) != own_slot
    }


class FunnelCounter:
    """The filters of AC-21 in order (AC-24). Games are added one at a time, so a long run holds only
    the games that pass, not every game IGDB sent; with `keep_candidates=False` (the counts-only
    mode of `--counts`) it holds none."""

    def __init__(
        self,
        *,
        today: dt.date,
        settings: ContentSettings,
        earlier_answer_igdb_ids: Collection[int],
        never_igdb_ids: Collection[int],
        reserved_igdb_ids: Collection[int] = (),
        keep_candidates: bool = True,
    ) -> None:
        self._settings = settings
        self._keep_candidates = keep_candidates
        # The eligible screenshots of the game being added, made once for the filter and the
        # rebuild count.
        self._last: tuple[IgdbGame, list[IgdbScreenshot]] | None = None
        latest_release = today - dt.timedelta(days=settings.CANDIDATE_MIN_AGE_DAYS)
        min_screenshots = max(settings.CANDIDATE_MIN_SCREENSHOTS, REQUIRED_SCREENSHOTS)

        def released_in_time(game: IgdbGame) -> bool:
            if game.first_release_date is None:
                return False
            released = dt.datetime.fromtimestamp(game.first_release_date, dt.UTC).date()
            return released <= latest_release

        self._rules = [
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
                lambda game: len(self._eligible(game)) >= min_screenshots,
            ),
            ("a valid IGDB slug", lambda game: is_valid_slug(game.slug)),
            ("not rejected as never", lambda game: game.id not in never_igdb_ids),
            ("not reserved for a rebuild", lambda game: game.id not in reserved_igdb_ids),
            ("not an earlier answer", lambda game: game.id not in earlier_answer_igdb_ids),
        ]
        # Index 0 is the games read; index n the games that passed the first n filters.
        self._counts = [0] * (len(self._rules) + 1)
        self._candidates: list[IgdbGame] = []
        self._survive_one_rebuild = 0

    def add(self, game: IgdbGame) -> None:
        self._counts[0] += 1
        for number, (_, keep) in enumerate(self._rules, start=1):
            if not keep(game):
                return
            self._counts[number] += 1
        if self._keep_candidates:
            self._candidates.append(game)
        if len(self._eligible(game)) >= REBUILD_SCREENSHOTS:
            self._survive_one_rebuild += 1

    def _eligible(self, game: IgdbGame) -> list[IgdbScreenshot]:
        if self._last is None or self._last[0] is not game:
            self._last = (game, eligible_screenshots(game, self._settings))
        return self._last[1]

    def result(self) -> Funnel:
        names = ["games read"] + [name for name, _ in self._rules]
        steps = tuple(
            FunnelStep(name, count) for name, count in zip(names, self._counts, strict=True)
        )
        return Funnel(steps, tuple(self._candidates), self._survive_one_rebuild)


def funnel(
    games: Iterable[IgdbGame],
    *,
    today: dt.date,
    settings: ContentSettings,
    earlier_answer_igdb_ids: Collection[int],
    never_igdb_ids: Collection[int],
    reserved_igdb_ids: Collection[int] = (),
) -> Funnel:
    counter = FunnelCounter(
        today=today,
        settings=settings,
        earlier_answer_igdb_ids=earlier_answer_igdb_ids,
        never_igdb_ids=never_igdb_ids,
        reserved_igdb_ids=reserved_igdb_ids,
    )
    for game in games:
        counter.add(game)
    return counter.result()


def choose_answer(candidates: Sequence[IgdbGame], stream: random.Random) -> IgdbGame:
    """One candidate, drawn from the list sorted by IGDB id (AC-26, AC-28)."""
    if not candidates:
        raise NotBuildableError(NotBuildableReason.NO_CANDIDATE, "no candidate is left")
    ordered = sorted(candidates, key=lambda game: game.id)
    return ordered[draw_index(stream, len(ordered))]


@dataclass(frozen=True)
class ScreenshotPlan:
    """Which screenshot goes where. Every one is used once: the step-1 crop is never one of the
    four full screenshots of steps 2 to 5 (AC-34), and the clip uses none of the five (AC-37)."""

    steps: tuple[IgdbScreenshot, ...]  # steps 2 to 5, in step order
    crop: IgdbScreenshot  # step 1
    clip: tuple[IgdbScreenshot, ...]  # step 6, two or three

    @property
    def image_ids(self) -> list[str]:
        """Every image id the draft used (the crop, steps 2 to 5, then the clip). A rejected draft's
        `answer_exclusions.image_ids` holds all of them, and every later build of the slot leaves
        them out (`rebuild_avoid_ids`)."""
        return [shot.image_id for shot in (self.crop, *self.steps, *self.clip)]


def rebuild_avoid_ids(exclusions: Iterable[ExclusionRow], *, variant: str, slot: int) -> set[str]:
    """The screenshots a build of this slot must not use: the union of `image_ids` of every
    `rebuild` row of the slot, so no rebuild shows a screenshot of any rejected draft (AC-52,
    decision 71). Empty when the slot has no such row. A `rebuild` row with no image ids means the
    reject step did not record them; rebuilding would repeat the rejected draft, so it is an error.
    """
    avoid: set[str] = set()
    for row in exclusions:
        if row.reason != ExclusionReason.REBUILD or (row.variant, row.build_slot) != (
            variant,
            slot,
        ):
            continue
        if not row.image_ids:
            raise NotBuildableError(
                NotBuildableReason.BAD_REBUILD_ROW,
                f"the rebuild row of slot {slot} has no image ids; the reject step did not record them",
            )
        avoid.update(row.image_ids)
    return avoid


def assign_screenshots(
    game: IgdbGame,
    stream: random.Random,
    avoid_ids: Collection[str],
    *,
    settings: ContentSettings,
) -> ScreenshotPlan:
    """Pick the screenshots of one build from the eligible ones, minus `avoid_ids` (for a rebuild,
    `rebuild_avoid_ids`, R-9). Fewer than `REQUIRED_SCREENSHOTS` left is `NotBuildableError`: reason
    `cannot_rebuild` for a rebuild, `too_few_screenshots` otherwise."""
    pool = [shot for shot in eligible_screenshots(game, settings) if shot.image_id not in avoid_ids]
    if len(pool) < REQUIRED_SCREENSHOTS:
        if avoid_ids:
            raise NotBuildableError(
                NotBuildableReason.CANNOT_REBUILD,
                f"this slot cannot be rebuilt: {len(pool)} usable screenshots are left after"
                f" leaving out the {len(avoid_ids)} of the rejected drafts and"
                f" {REQUIRED_SCREENSHOTS} are needed; give up the rebuild with build_puzzles.py"
                " (M1-22d)",
            )
        raise NotBuildableError(
            NotBuildableReason.TOO_FEW_SCREENSHOTS,
            f"{len(pool)} usable screenshots, {REQUIRED_SCREENSHOTS} are needed",
        )
    # A rebuild needs 7 screenshots the draft did not use, so the clip takes a third one only when
    # that many are still left (a game with 14 usable screenshots survives one rebuild, `--counts`).
    spare_for_clip = len(pool) - STEP_SCREENSHOTS - CROP_SCREENSHOTS - REQUIRED_SCREENSHOTS
    max_clip = max(MIN_CLIP_SCREENSHOTS, min(MAX_CLIP_SCREENSHOTS, spare_for_clip))
    clip_count = min(MIN_CLIP_SCREENSHOTS + draw_index(stream, 2), max_clip)
    # One draw without replacement, so no screenshot can be in two places.
    picks = draw_distinct(stream, pool, STEP_SCREENSHOTS + CROP_SCREENSHOTS + clip_count)
    return ScreenshotPlan(
        steps=tuple(picks[:STEP_SCREENSHOTS]),
        crop=picks[STEP_SCREENSHOTS],
        clip=tuple(picks[STEP_SCREENSHOTS + CROP_SCREENSHOTS :]),
    )
