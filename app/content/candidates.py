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
# The reviewer sees steps 1 to 5 as stills. A rebuild must not show any of them again (AC-52); the
# clip is a few seconds of pan and fade, so it may reuse any screenshot.
STILL_SCREENSHOTS = STEP_SCREENSHOTS + CROP_SCREENSHOTS
# So a game that survives one rebuild has this many usable screenshots: 5 to leave out, 7 to build.
REBUILD_SCREENSHOTS = STILL_SCREENSHOTS + REQUIRED_SCREENSHOTS


class NotBuildableError(Exception):
    """No answer or no screenshot set is left. The message holds no game name."""


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
    the games that pass, not every game IGDB sent."""

    def __init__(
        self,
        *,
        today: dt.date,
        settings: ContentSettings,
        earlier_answer_igdb_ids: Collection[int],
        never_igdb_ids: Collection[int],
        reserved_igdb_ids: Collection[int] = (),
    ) -> None:
        self._settings = settings
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
                lambda game: len(eligible_screenshots(game, settings)) >= min_screenshots,
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
        self._candidates.append(game)
        if len(eligible_screenshots(game, self._settings)) >= REBUILD_SCREENSHOTS:
            self._survive_one_rebuild += 1

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
        raise NotBuildableError("no candidate is left")
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
        """Every image id the draft used, in step order: the crop (step 1), steps 2 to 5, then the
        clip. A rejected draft's `answer_exclusions.image_ids` holds this list, and a rebuild reads
        the stills back with `rebuild_avoid_ids`."""
        return [shot.image_id for shot in (self.crop, *self.steps, *self.clip)]


def rebuild_avoid_ids(image_ids: Sequence[str]) -> list[str]:
    """The screenshots a rebuild must not use: the ones the reviewer saw as stills (steps 1 to 5),
    the first `STILL_SCREENSHOTS` of the list `ScreenshotPlan.image_ids` made. The clip's may be
    used again (AC-52)."""
    return list(image_ids[:STILL_SCREENSHOTS])


def assign_screenshots(
    game: IgdbGame,
    stream: random.Random,
    avoid_ids: Collection[str],
    *,
    settings: ContentSettings,
) -> ScreenshotPlan:
    """Pick the screenshots of one build from the eligible ones, minus `avoid_ids` (for a rebuild,
    `rebuild_avoid_ids` of the rejected draft, R-9). Fewer than `REQUIRED_SCREENSHOTS` left is
    `NotBuildableError`; for a rebuild the message says to reject the draft as "never"."""
    pool = [shot for shot in eligible_screenshots(game, settings) if shot.image_id not in avoid_ids]
    if len(pool) < REQUIRED_SCREENSHOTS:
        reason = f"{len(pool)} usable screenshots, {REQUIRED_SCREENSHOTS} are needed"
        if avoid_ids:
            reason += (
                f" after leaving out the {len(avoid_ids)} of the rejected draft: this game has too"
                f" few screenshots for a rebuild ({REBUILD_SCREENSHOTS} are needed); reject the"
                ' draft as "never"'
            )
        raise NotBuildableError(reason)
    spare = len(pool) - STEP_SCREENSHOTS - CROP_SCREENSHOTS
    clip_count = min(MIN_CLIP_SCREENSHOTS + draw_index(stream, 2), spare)
    # One draw without replacement, so no screenshot can be in two places.
    picks = draw_distinct(stream, pool, STEP_SCREENSHOTS + CROP_SCREENSHOTS + clip_count)
    return ScreenshotPlan(
        steps=tuple(picks[:STEP_SCREENSHOTS]),
        crop=picks[STEP_SCREENSHOTS],
        clip=tuple(picks[STEP_SCREENSHOTS + CROP_SCREENSHOTS :]),
    )
