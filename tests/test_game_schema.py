"""Database constraints of the game tables on real Postgres (AC-9 to AC-11, AC-13, AC-13b, AC-18).
Each rejected row names the constraint it must hit, so a row that fails for another reason does not
pass."""

import datetime as dt

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.title import create_title
from app.models.exclusion import AnswerExclusion
from app.models.game import Game
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue
from app.models.title import Title
from tests.factories import create_game, create_puzzle, create_titles

PUZZLE_DATE = dt.date(2026, 11, 3)


async def assert_rejected(db: AsyncSession, row: object, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with db.begin_nested():
            db.add(row)
            await db.flush()


async def assert_statement_rejected(db: AsyncSession, statement: object, constraint: str) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        async with db.begin_nested():
            await db.execute(statement)  # type: ignore[arg-type]


def new_game(**overrides: object) -> Game:
    values: dict[str, object] = {
        "slug": "game-guesser",
        "default_variant": "default",
        "title_kind": "video_game",
        "launch_date": dt.date(2026, 1, 1),
    }
    values.update(overrides)
    return Game(**values)


def new_puzzle(like: Puzzle, **overrides: object) -> Puzzle:
    """A draft with no date, in the game, variant and answer of `like`."""
    values: dict[str, object] = {
        "game_id": like.game_id,
        "variant": like.variant,
        "date": None,
        "status": "draft",
        "answer_title_id": like.answer_title_id,
    }
    values.update(overrides)
    return Puzzle(**values)


def new_title(**overrides: object) -> Title:
    values: dict[str, object] = {
        "kind": "video_game",
        "name": "Portal",
        "normalized_name": "portal",
    }
    values.update(overrides)
    return Title(**values)


def new_exclusion(like: Puzzle, **overrides: object) -> AnswerExclusion:
    values: dict[str, object] = {
        "game_id": like.game_id,
        "variant": like.variant,
        "title_id": like.answer_title_id,
        "reason": "never",
    }
    values.update(overrides)
    return AnswerExclusion(**values)


def new_media(clue_id: int, **overrides: object) -> ClueMediaFile:
    values: dict[str, object] = {
        "clue_id": clue_id,
        "role": "source",
        "key": "a" * 64,
        "content_type": "image/webp",
        "byte_size": 1000,
        "width": 640,
        "height": 360,
        "duration_ms": None,
    }
    values.update(overrides)
    return ClueMediaFile(**values)


async def clue_at(db: AsyncSession, puzzle: Puzzle, step: int) -> PuzzleClue:
    clue = await db.scalar(
        select(PuzzleClue).where(PuzzleClue.puzzle_id == puzzle.id, PuzzleClue.step == step)
    )
    assert clue is not None
    return clue


async def count(db: AsyncSession, model: type) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
async def puzzle(db_session: AsyncSession) -> Puzzle:
    game = await create_game(db_session)
    (answer,) = await create_titles(db_session, "Half-Life 2")
    return await create_puzzle(
        db_session, game=game, answer=answer, puzzle_date=PUZZLE_DATE, with_media=False
    )


async def test__game__a_valid_row_is_accepted(db_session: AsyncSession) -> None:
    game = await create_game(db_session, slug="a1-b2-c3")

    assert game.id is not None
    assert game.is_enabled is True
    assert game.created_at is not None


@pytest.mark.parametrize(
    "slug",
    ["Game-Guesser", "-game", "game-", "game--guesser", "game guesser", "game_guesser", ""],
    ids=[
        "upper_case",
        "leading_hyphen",
        "trailing_hyphen",
        "double_hyphen",
        "space",
        "underscore",
        "empty",
    ],
)
async def test__game_slug__bad_format_is_rejected(db_session: AsyncSession, slug: str) -> None:
    await assert_rejected(db_session, new_game(slug=slug), "ck_games_slug_format")


async def test__game_slug__duplicate_is_rejected(db_session: AsyncSession) -> None:
    await create_game(db_session, slug="game-guesser")

    await assert_rejected(db_session, new_game(slug="game-guesser"), "uq_games_slug")


async def test__title__a_valid_row_is_accepted(db_session: AsyncSession) -> None:
    title = await create_title(db_session, name="Half-Life 2", franchise="Half-Life", year=2004)

    assert title.id is not None
    assert title.kind == "video_game"


async def test__title_kind__other_than_video_game_is_rejected(db_session: AsyncSession) -> None:
    title = Title(kind="movie", name="Alien", normalized_name="alien")

    await assert_rejected(db_session, title, "ck_titles_kind")


async def test__puzzle__a_valid_row_is_accepted(puzzle: Puzzle) -> None:
    assert puzzle.id is not None
    assert puzzle.status == "scheduled"


async def test__puzzle_status__outside_the_allowed_set_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    bad = Puzzle(
        game_id=puzzle.game_id,
        variant="default",
        date=PUZZLE_DATE + dt.timedelta(days=1),
        status="published",
        answer_title_id=puzzle.answer_title_id,
    )

    await assert_rejected(db_session, bad, "ck_puzzles_status")


async def test__puzzle__same_game_variant_and_date_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    duplicate = Puzzle(
        game_id=puzzle.game_id,
        variant=puzzle.variant,
        date=puzzle.date,
        status="draft",
        answer_title_id=puzzle.answer_title_id,
    )

    await assert_rejected(db_session, duplicate, "uq_puzzles_game_id_variant_date")


async def test__content_rows__valid_rows_without_a_date_or_with_a_slot_are_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    db_session.add_all(
        [
            new_puzzle(puzzle, status="draft"),
            new_puzzle(puzzle, status="draft"),
            new_puzzle(puzzle, status="approved", build_slot=1),
            new_exclusion(puzzle, reason="rebuild", build_slot=1, image_ids=["a1", "b2"]),
            new_exclusion(puzzle, reason="never"),
        ]
    )
    await db_session.flush()

    assert await count(db_session, Puzzle) == 4
    assert await count(db_session, AnswerExclusion) == 2


# (a row that must already exist, the rule-breaking row, the constraint it hits)
CONTENT_REJECTIONS = {
    "scheduled_without_date": (
        None,
        lambda puzzle: new_puzzle(puzzle, status="scheduled"),
        "ck_puzzles_scheduled_has_date",
    ),
    "slot_zero": (
        None,
        lambda puzzle: new_puzzle(puzzle, build_slot=0),
        "ck_puzzles_build_slot_positive",
    ),
    "slot_taken": (
        lambda puzzle: new_puzzle(puzzle, build_slot=7),
        lambda puzzle: new_puzzle(puzzle, build_slot=7),
        "uq_puzzles_game_id_variant_build_slot",
    ),
    "igdb_id_taken": (
        lambda puzzle: new_title(igdb_id=1942),
        lambda puzzle: new_title(igdb_id=1942, name="Other", normalized_name="other"),
        "uq_titles_igdb_id",
    ),
    "exclusion_reason": (
        None,
        lambda puzzle: new_exclusion(puzzle, reason="maybe"),
        "ck_answer_exclusions_reason",
    ),
    "exclusion_slot_zero": (
        None,
        lambda puzzle: new_exclusion(puzzle, reason="rebuild", build_slot=0),
        "ck_answer_exclusions_build_slot_positive",
    ),
    "rebuild_without_slot": (
        None,
        lambda puzzle: new_exclusion(puzzle, reason="rebuild"),
        "ck_answer_exclusions_rebuild_has_slot",
    ),
    "never_with_slot": (
        None,
        lambda puzzle: new_exclusion(puzzle, reason="never", build_slot=3),
        "ck_answer_exclusions_never_has_no_slot",
    ),
}


@pytest.mark.parametrize("case", list(CONTENT_REJECTIONS))
async def test__content_columns__rule_breaking_row_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle, case: str
) -> None:
    existing, bad_row, constraint = CONTENT_REJECTIONS[case]
    if existing is not None:
        db_session.add(existing(puzzle))
        await db_session.flush()

    await assert_rejected(db_session, bad_row(puzzle), constraint)


async def test__puzzle__same_date_in_another_variant_or_game_is_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    other_game = await create_game(db_session, slug="other-game")
    answer = await db_session.get(Title, puzzle.answer_title_id)
    assert answer is not None

    await create_puzzle(
        db_session, game=other_game, answer=answer, puzzle_date=puzzle.date, variant="default"
    )
    game = await db_session.get(Game, puzzle.game_id)
    assert game is not None
    await create_puzzle(
        db_session, game=game, answer=answer, puzzle_date=puzzle.date, variant="hard"
    )


async def test__delete_game__refused_while_it_has_puzzles(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    await assert_statement_rejected(
        db_session, delete(Game).where(Game.id == puzzle.game_id), "fk_puzzles_game_id_games"
    )


async def test__puzzle_clue__six_valid_steps_are_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    assert await count(db_session, PuzzleClue) == 6
    assert (await clue_at(db_session, puzzle, 3)).hint_kind is None


@pytest.mark.parametrize("step", [-1, 0, 7])
async def test__puzzle_clue_step__outside_1_to_6_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle, step: int
) -> None:
    clue = PuzzleClue(puzzle_id=puzzle.id, step=step, media_type="image")

    await assert_rejected(db_session, clue, "ck_puzzle_clues_step")


async def test__puzzle_clue_step__duplicate_in_a_puzzle_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    clue = PuzzleClue(puzzle_id=puzzle.id, step=1, media_type="image")

    await assert_rejected(db_session, clue, "uq_puzzle_clues_puzzle_id_step")


async def test__puzzle_clue_media_type__other_than_image_or_video_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    await db_session.execute(delete(PuzzleClue).where(PuzzleClue.step == 1))
    clue = PuzzleClue(puzzle_id=puzzle.id, step=1, media_type="audio")

    await assert_rejected(db_session, clue, "ck_puzzle_clues_media_type")


@pytest.mark.parametrize(
    ("hint_kind", "hint_value"), [("year", None), (None, "2004")], ids=["kind_only", "value_only"]
)
async def test__puzzle_clue_hint__only_one_half_set_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle, hint_kind: str | None, hint_value: str | None
) -> None:
    await db_session.execute(delete(PuzzleClue).where(PuzzleClue.step == 1))
    clue = PuzzleClue(
        puzzle_id=puzzle.id,
        step=1,
        media_type="image",
        hint_kind=hint_kind,
        hint_value=hint_value,
    )

    await assert_rejected(db_session, clue, "ck_puzzle_clues_hint_pair")


async def test__clue_media_file__valid_image_and_video_rows_are_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    image_clue = await clue_at(db_session, puzzle, 1)
    video_clue = await clue_at(db_session, puzzle, 6)

    db_session.add_all(
        [
            new_media(image_clue.id),
            new_media(video_clue.id, content_type="video/mp4", duration_ms=12_000),
            new_media(video_clue.id, content_type="video/webm", duration_ms=12_000),
            new_media(video_clue.id, role="poster", key="b" * 64),
        ]
    )
    await db_session.flush()

    assert await count(db_session, ClueMediaFile) == 4


async def test__clue_media_file__same_key_on_two_clues_is_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    first = await clue_at(db_session, puzzle, 1)
    second = await clue_at(db_session, puzzle, 2)

    db_session.add_all([new_media(first.id), new_media(second.id)])
    await db_session.flush()


async def test__clue_media_file__smallest_valid_values_and_a_large_file_are_accepted(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    clue = await clue_at(db_session, puzzle, 6)

    db_session.add_all(
        [
            new_media(clue.id, byte_size=1, width=1, height=1),
            new_media(
                clue.id,
                content_type="video/mp4",
                duration_ms=1,
                byte_size=5_000_000_000,
                key="b" * 64,
            ),
        ]
    )
    await db_session.flush()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"role": "thumbnail"}, "ck_clue_media_files_role"),
        ({"content_type": "image/png"}, "ck_clue_media_files_content_type"),
        ({"duration_ms": 1000}, "ck_clue_media_files_duration_video_only"),
        ({"content_type": "video/mp4"}, "ck_clue_media_files_duration_video_only"),
        (
            {"role": "poster", "content_type": "video/mp4", "duration_ms": 1000},
            "ck_clue_media_files_poster_is_webp",
        ),
        ({"byte_size": 0}, "ck_clue_media_files_byte_size_positive"),
        ({"width": 0}, "ck_clue_media_files_width_positive"),
        ({"height": -1}, "ck_clue_media_files_height_positive"),
        (
            {"content_type": "video/mp4", "duration_ms": 0},
            "ck_clue_media_files_duration_ms_positive",
        ),
    ],
    ids=[
        "bad_role",
        "bad_content_type",
        "duration_on_an_image",
        "video_without_duration",
        "poster_not_webp",
        "zero_byte_size",
        "zero_width",
        "negative_height",
        "zero_duration",
    ],
)
async def test__clue_media_file__rule_breaking_row_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle, overrides: dict[str, object], constraint: str
) -> None:
    clue = await clue_at(db_session, puzzle, 6)

    await assert_rejected(db_session, new_media(clue.id, **overrides), constraint)


async def test__clue_media_file__second_source_of_a_content_type_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    clue = await clue_at(db_session, puzzle, 1)
    db_session.add(new_media(clue.id))
    await db_session.flush()

    await assert_rejected(
        db_session,
        new_media(clue.id, key="c" * 64),
        "uq_clue_media_files_clue_id_content_type_source",
    )


async def test__clue_media_file__second_poster_on_a_clue_is_rejected(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    clue = await clue_at(db_session, puzzle, 6)
    db_session.add(new_media(clue.id, role="poster"))
    await db_session.flush()

    await assert_rejected(
        db_session,
        new_media(clue.id, role="poster", key="c" * 64),
        "uq_clue_media_files_clue_id_poster",
    )


async def test__clue_media_file__poster_does_not_clash_with_a_webp_source(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    clue = await clue_at(db_session, puzzle, 6)

    db_session.add_all(
        [new_media(clue.id, role="source"), new_media(clue.id, role="poster", key="b" * 64)]
    )
    await db_session.flush()


async def test__delete_puzzle__removes_its_clues_and_their_media(
    db_session: AsyncSession, puzzle: Puzzle
) -> None:
    video_clue = await clue_at(db_session, puzzle, 6)
    db_session.add_all(
        [
            new_media(video_clue.id, content_type="video/mp4", duration_ms=12_000),
            new_media(video_clue.id, role="poster", key="b" * 64),
        ]
    )
    await db_session.flush()
    assert await count(db_session, PuzzleClue) == 6
    assert await count(db_session, ClueMediaFile) == 2

    await db_session.execute(delete(Puzzle).where(Puzzle.id == puzzle.id))

    assert await count(db_session, PuzzleClue) == 0
    assert await count(db_session, ClueMediaFile) == 0
    assert await count(db_session, Game) == 1
    assert await count(db_session, Title) == 1
