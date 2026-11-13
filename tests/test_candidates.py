"""The candidate filters, the screenshot assignment and the earlier-answer queries (M1-22b,
SPEC-04 AC-21 to AC-25, AC-34, AC-37, R-20).

The funnel and the assignment are pure; the last test uses the database. No network."""

import datetime as dt
from collections import Counter
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.content.candidates import (
    CANDIDATE_TYPES,
    REBUILD_SCREENSHOTS,
    REQUIRED_SCREENSHOTS,
    FunnelCounter,
    NotBuildableError,
    NotBuildableReason,
    assign_screenshots,
    candidates_where,
    choose_answer,
    funnel,
    never_igdb_ids,
    rebuild_avoid_ids,
    rebuild_reserved_igdb_ids,
)
from app.content.igdb import IgdbGame, parse_game
from app.content.seed import TEST_SALT, Purpose, SlotSeed
from app.content.settings import ContentSettings
from app.content.titles_import import IN_SCOPE_TYPES, in_scope
from app.crud.exclusion import ExclusionRow, list_exclusions
from app.crud.puzzle import list_answer_igdb_ids
from app.crud.title import create_title
from app.models.exclusion import AnswerExclusion
from app.models.puzzle import PuzzleStatus
from tests.factories import create_game, create_puzzle

TODAY = dt.date(2026, 11, 13)
SETTINGS = ContentSettings(_env_file=None)


def shots(
    count: int, *, width: int = 1920, height: int = 1080, start: int = 0
) -> list[dict[str, Any]]:
    return [
        {"image_id": f"shot{number:02d}", "width": width, "height": height}
        for number in range(start, start + count)
    ]


def released(days_ago: int) -> int:
    day = TODAY - dt.timedelta(days=days_ago)
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())


def make_game(igdb_id: int = 1, **fields: Any) -> IgdbGame:
    """A game that passes every filter, with the fields of the test changed."""
    raw = {
        "id": igdb_id,
        "name": f"Game {igdb_id}",
        "slug": f"game-{igdb_id}",
        "game_type": 0,
        "first_release_date": released(2000),
        "total_rating_count": 100,
        "screenshots": shots(8),
        **fields,
    }
    game = parse_game(raw, max_year=2027, skips=Counter(), drops=Counter())
    assert game is not None
    return game


def passed(games: list[IgdbGame], **funnel_args: Any) -> list[int]:
    args: dict[str, Any] = {"earlier_answer_igdb_ids": set(), "never_igdb_ids": set()} | funnel_args
    return [step.passed for step in funnel(games, today=TODAY, settings=SETTINGS, **args).steps]


FILTER_COUNT = 8  # type, age, ratings, screenshots, slug, never, reserved for a rebuild, earlier
# Each row: a game, the funnel arguments, and the filter (1 to 8) that drops it. 0 means it passes.
ROWS: list[tuple[str, dict[str, Any], dict[str, Any], int]] = [
    ("an edition of another game", {"version_parent": 9}, {}, 1),
    ("a DLC", {"game_type": 1}, {}, 1),
    ("a standalone expansion", {"game_type": 4}, {}, 1),
    ("a remake", {"game_type": 8}, {}, 0),
    ("released 364 days ago", {"first_release_date": released(364)}, {}, 2),
    ("released 365 days ago", {"first_release_date": released(365)}, {}, 0),
    ("no release date", {"first_release_date": None}, {}, 2),
    ("49 ratings", {"total_rating_count": 49}, {}, 3),
    ("50 ratings", {"total_rating_count": 50}, {}, 0),
    (
        "49 total ratings, 80 critic ratings",
        {"total_rating_count": 49, "aggregated_rating_count": 80},
        {},
        3,
    ),
    ("6 screenshots", {"screenshots": shots(6)}, {}, 4),
    ("7 screenshots", {"screenshots": shots(7, width=1280, height=720)}, {}, 0),
    (
        "7 screenshots, one 1279 wide",
        {"screenshots": shots(6) + shots(1, width=1279, start=6)},
        {},
        4,
    ),
    (
        "7 screenshots, one 719 high",
        {"screenshots": shots(6) + shots(1, height=719, start=6)},
        {},
        4,
    ),
    ("a slug with a capital", {"slug": "Game-1"}, {}, 5),
    ("a slug with a trailing newline", {"slug": "game-1\n"}, {}, 5),
    ("rejected as never", {}, {"never_igdb_ids": {2}}, 6),
    ("reserved for a rebuild that is not built yet", {}, {"reserved_igdb_ids": {2}}, 7),
    ("an earlier answer", {}, {"earlier_answer_igdb_ids": {2}}, 8),
]


@pytest.mark.parametrize(
    ("fields", "funnel_args", "dropped_at"), [r[1:] for r in ROWS], ids=[r[0] for r in ROWS]
)
def test__funnel__each_filter_drops_its_game_and_counts_add_up(
    fields: dict[str, Any], funnel_args: dict[str, Any], dropped_at: int
) -> None:
    # Two games share one set of screenshot ids: the filter must not mix them up.
    games = [make_game(1), make_game(2, **fields)]
    counts = passed(games, **funnel_args)
    # The first count is the games read; each filter keeps what the one before kept.
    keeps_game_two = FILTER_COUNT + 1 if dropped_at == 0 else dropped_at
    assert counts == [2] * keeps_game_two + [1] * (FILTER_COUNT + 1 - keeps_game_two)


def test__funnel__thresholds_are_settings_and_candidates_are_in_the_search_list() -> None:
    assert (
        SETTINGS.CANDIDATE_MIN_RATINGS,
        SETTINGS.CANDIDATE_MIN_SCREENSHOTS,
        SETTINGS.CANDIDATE_MIN_WIDTH,
        SETTINGS.CANDIDATE_MIN_HEIGHT,
        SETTINGS.CANDIDATE_MIN_AGE_DAYS,
        SETTINGS.CANDIDATE_RATING_FIELD,
    ) == (50, 7, 1280, 720, 365, "total_rating_count")
    games = [make_game(1, total_rating_count=20), make_game(2, screenshots=shots(8, width=1000))]
    loose = SETTINGS.model_copy(update={"CANDIDATE_MIN_RATINGS": 10, "CANDIDATE_MIN_WIDTH": 1000})
    result = funnel(
        games, today=TODAY, settings=loose, earlier_answer_igdb_ids=(), never_igdb_ids=()
    )
    assert [game.id for game in result.candidates] == [1, 2]
    # R-20: the search list takes every candidate, so the import scope must cover the types.
    assert CANDIDATE_TYPES <= IN_SCOPE_TYPES
    assert all(in_scope(game) for game in result.candidates)
    # The run asks IGDB only for rated candidate types (the import's floor of 1 rating, R-20).
    assert candidates_where(SETTINGS) == (
        "game_type = (0,8,9,10) & version_parent = null & total_rating_count >= 1"
    )
    assert candidates_where(
        loose.model_copy(update={"CANDIDATE_RATING_FIELD": "aggregated_rating_count"})
    ).endswith("aggregated_rating_count >= 1")
    # A bad value is refused when the settings load. Ratings must stay at 1 or more (R-20).
    for name, bad in [
        ("CANDIDATE_MIN_RATINGS", 0),
        ("CANDIDATE_MIN_AGE_DAYS", -1),
        ("CANDIDATE_MIN_AGE_DAYS", 4000),
        ("CANDIDATE_MIN_WIDTH", 0),
        ("CANDIDATE_MIN_HEIGHT", 0),
        ("CANDIDATE_MIN_SCREENSHOTS", 0),
        ("CANDIDATE_RATING_FIELD", "rating_count"),
    ]:
        with pytest.raises(ValidationError, match=name):
            ContentSettings(**{name: bad}, _env_file=None)  # type: ignore[arg-type]
    assert ContentSettings(CANDIDATE_MIN_RATINGS=1, CANDIDATE_MIN_AGE_DAYS=0, _env_file=None)


@pytest.mark.parametrize("screenshot_count", [7, 8, 14, 15, 20])
def test__assign_screenshots__crop_and_clip_never_reuse_a_step_screenshot(
    screenshot_count: int,
) -> None:
    game = make_game(screenshots=shots(screenshot_count))
    clip_sizes = set()
    for slot in range(1, 41):
        stream = SlotSeed(
            SecretStr(TEST_SALT), "game-guesser", "default", slot, "1", other_secrets=()
        ).stream(Purpose.SCREENSHOTS)
        plan = assign_screenshots(game, stream, set(), settings=SETTINGS)
        step_ids = [shot.image_id for shot in plan.steps]
        clip_ids = [shot.image_id for shot in plan.clip]
        assert len(step_ids) == 4
        assert plan.crop.image_id not in step_ids  # AC-34
        assert not set(clip_ids) & {plan.crop.image_id, *step_ids}  # AC-37
        assert len(set(step_ids + clip_ids + [plan.crop.image_id])) == len(step_ids + clip_ids) + 1
        clip_sizes.add(len(clip_ids))
    # Three clip shots only when 7 more are left for a rebuild: a game with 14 keeps clip 2.
    assert clip_sizes == ({2} if screenshot_count <= 14 else {2, 3})


def test__assign_screenshots__skips_avoided_ones_and_ignores_input_order() -> None:
    game = make_game(screenshots=shots(10))
    stream = SlotSeed(
        SecretStr(TEST_SALT), "game-guesser", "default", 1, "1", other_secrets=()
    ).stream
    avoid = {"shot00", "shot01", "shot02"}
    plan = assign_screenshots(game, stream(Purpose.SCREENSHOTS), avoid, settings=SETTINGS)
    used = {shot.image_id for shot in (*plan.steps, plan.crop, *plan.clip)}
    assert not used & avoid
    shuffled = make_game(screenshots=list(reversed(shots(10))))
    again = assign_screenshots(shuffled, stream(Purpose.SCREENSHOTS), avoid, settings=SETTINGS)
    assert again == plan
    with pytest.raises(NotBuildableError):  # 7 are left after 3 are avoided, 6 after a fourth
        assign_screenshots(game, stream(Purpose.SCREENSHOTS), avoid | {"shot03"}, settings=SETTINGS)
    assert REQUIRED_SCREENSHOTS == 7
    candidates = [make_game(number) for number in (5, 3, 9, 1)]
    answer = choose_answer(candidates, stream(Purpose.ANSWER))
    assert choose_answer(list(reversed(candidates)), stream(Purpose.ANSWER)) == answer
    with pytest.raises(NotBuildableError):
        choose_answer([], stream(Purpose.ANSWER))


def test__assign_screenshots__golden_plans_do_not_change_with_the_python_version() -> None:
    """The draws use only `random()`, so these ids hold on any Python. If this goes red, the choice
    logic changed: raise the builder's GENERATOR_VERSION."""

    def plan(count: int, slot: int) -> list[str]:
        stream = SlotSeed(
            SecretStr(TEST_SALT), "game-guesser", "default", slot, "1", other_secrets=()
        ).stream(Purpose.SCREENSHOTS)
        game = make_game(screenshots=shots(count))
        return assign_screenshots(game, stream, set(), settings=SETTINGS).image_ids

    assert [plan(10, slot) for slot in (1, 2, 3)] == [
        ["shot07", "shot08", "shot03", "shot04", "shot05", "shot06", "shot09"],
        ["shot08", "shot00", "shot03", "shot01", "shot04", "shot05", "shot06"],
        ["shot09", "shot00", "shot01", "shot07", "shot08", "shot06", "shot04"],
    ]
    # 20 screenshots: slot 1 draws a clip of three (the cap allows it from 15), slot 2 of two.
    assert [plan(20, slot) for slot in (1, 2)] == [
        ["shot15", "shot17", "shot07", "shot08", "shot09", "shot14", "shot13", "shot16"],
        ["shot17", "shot00", "shot06", "shot02", "shot04", "shot07", "shot10"],
    ]


def rebuild_row(slot: int, image_ids: list[str], reason: str = "rebuild") -> ExclusionRow:
    return ExclusionRow(1, 100, reason, "default", slot, image_ids)


def test__rebuild__leaves_out_every_screenshot_of_every_rejected_draft() -> None:
    game = make_game(screenshots=shots(14))  # 7 for the draft and 7 for one rebuild
    first = assign_screenshots(game, stream_of(1, "1"), set(), settings=SETTINGS)
    assert len(first.image_ids) == 7  # a game with 14 keeps 7 back for a rebuild
    rows = [
        rebuild_row(1, first.image_ids),
        rebuild_row(2, ["shot13"]),  # another slot's rows do not count
        rebuild_row(1, ["shot99"], "never"),  # a never row names an image that is in no pool
    ]
    avoid = rebuild_avoid_ids(rows, variant="default", slot=1)
    assert avoid == set(first.image_ids)
    assert rebuild_avoid_ids(rows, variant="other", slot=1) == set()
    second = assign_screenshots(game, stream_of(1, "2"), avoid, settings=SETTINGS)
    assert not set(second.image_ids) & avoid
    # A second rejected draft of the slot: the union of both is left out, and 14 are gone.
    rows.append(rebuild_row(1, second.image_ids))
    union = rebuild_avoid_ids(rows, variant="default", slot=1)
    assert union == set(first.image_ids) | set(second.image_ids)
    with pytest.raises(NotBuildableError, match="give up the rebuild") as refusal:
        assign_screenshots(game, stream_of(1, "3"), union, settings=SETTINGS)
    assert refusal.value.reason == NotBuildableReason.CANNOT_REBUILD
    assert "reject the draft" not in str(refusal.value)  # the draft is already deleted
    # A row with no image ids: the reject step failed to record them; do not repeat the draft.
    with pytest.raises(NotBuildableError, match="no image ids") as bad:
        rebuild_avoid_ids([*rows, rebuild_row(1, [])], variant="default", slot=1)
    assert bad.value.reason == NotBuildableReason.BAD_REBUILD_ROW
    # Without a rebuild row there is nothing to avoid, and too few screenshots is another reason.
    assert rebuild_avoid_ids([rebuild_row(2, ["x"])], variant="default", slot=1) == set()
    with pytest.raises(NotBuildableError) as few:
        assign_screenshots(
            make_game(screenshots=shots(6)), stream_of(1, "1"), set(), settings=SETTINGS
        )
    assert few.value.reason == NotBuildableReason.TOO_FEW_SCREENSHOTS
    assert REBUILD_SCREENSHOTS == 14


def test__funnel_counter__counts_one_rebuild_at_14_screenshots_and_can_keep_no_games() -> None:
    games = [make_game(1, screenshots=shots(13)), make_game(2, screenshots=shots(14))]
    args: dict[str, Any] = {
        "today": TODAY,
        "settings": SETTINGS,
        "earlier_answer_igdb_ids": (),
        "never_igdb_ids": (),
    }
    kept = funnel(games, **args)
    assert len(kept.candidates) == 2 and kept.survive_one_rebuild == 1
    counter = FunnelCounter(**args, keep_candidates=False)
    for game in games:
        counter.add(game)
    result = counter.result()
    assert result.candidates == () and result.survive_one_rebuild == 1
    assert result.steps == kept.steps


def stream_of(slot: int, version: str) -> Any:
    seed = SlotSeed(SecretStr(TEST_SALT), "g", "default", slot, version, other_secrets=())
    return seed.stream(Purpose.SCREENSHOTS)


async def test__earlier_answers_and_exclusions__come_from_every_status(
    db_session: AsyncSession,
) -> None:
    game = await create_game(db_session)
    other_game = await create_game(db_session, slug="other-game")
    titles = [
        await create_title(db_session, name=f"Title {number}", igdb_id=number * 100)
        for number in range(1, 6)
    ]
    manual = await create_title(db_session, name="Made by hand")
    statuses = [PuzzleStatus.DRAFT, PuzzleStatus.IN_REVIEW, PuzzleStatus.APPROVED]
    for number, status in enumerate(statuses):
        await create_puzzle(
            db_session,
            game=game,
            answer=titles[number],
            puzzle_date=dt.date(2026, 1, 1) + dt.timedelta(days=number),
            status=status,
        )
    await create_puzzle(
        db_session, game=game, answer=titles[3], puzzle_date=dt.date(2026, 2, 1)
    )  # scheduled
    await create_puzzle(db_session, game=game, answer=manual, puzzle_date=dt.date(2026, 2, 2))
    await create_puzzle(
        db_session, game=other_game, answer=titles[4], puzzle_date=dt.date(2026, 2, 3)
    )
    assert await list_answer_igdb_ids(db_session, game.id) == {100, 200, 300, 400}
    # "never" rows can repeat; a "rebuild" row keeps its title a candidate.
    for reason, slot, title in [("never", None, titles[0]), ("never", None, titles[0])] + [
        ("rebuild", 7, titles[1]),
        ("never", None, manual),
    ]:
        db_session.add(
            AnswerExclusion(
                game_id=game.id,
                variant="default",
                title_id=title.id,
                reason=reason,
                build_slot=slot,
            )
        )
    await db_session.flush()
    rows = await list_exclusions(db_session, game.id)
    assert len(rows) == 4
    assert never_igdb_ids(rows) == {100}
    # The rebuild row reserves its title for slot 7 only: every other slot must not draw it.
    assert rebuild_reserved_igdb_ids(rows) == {200}
    assert rebuild_reserved_igdb_ids(rows, own_slot=("default", 8)) == {200}
    assert rebuild_reserved_igdb_ids(rows, own_slot=("default", 7)) == set()
    # This test's rebuild row holds no image ids, as if the reject step had not recorded them.
    with pytest.raises(NotBuildableError, match="no image ids"):
        rebuild_avoid_ids(rows, variant="default", slot=7)
