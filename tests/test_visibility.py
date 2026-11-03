import datetime as dt

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.puzzle import get_visible_puzzle_with_details, visible_puzzle_condition
from app.models.game import Game
from app.models.puzzle import Puzzle, PuzzleStatus
from app.models.title import Title
from tests.factories import create_game, create_puzzle, create_titles

TODAY = dt.date(2026, 11, 3)
LAUNCH = dt.date(2026, 10, 31)


@pytest.mark.parametrize(
    ("status", "puzzle_date", "is_enabled", "is_visible"),
    [
        (PuzzleStatus.SCHEDULED, TODAY, True, True),
        (PuzzleStatus.SCHEDULED, TODAY - dt.timedelta(days=1), True, True),
        (PuzzleStatus.SCHEDULED, LAUNCH, True, True),
        (PuzzleStatus.DRAFT, TODAY, True, False),
        (PuzzleStatus.IN_REVIEW, TODAY, True, False),
        (PuzzleStatus.APPROVED, TODAY, True, False),
        (PuzzleStatus.SCHEDULED, TODAY + dt.timedelta(days=1), True, False),
        (PuzzleStatus.SCHEDULED, LAUNCH - dt.timedelta(days=1), True, False),
        (PuzzleStatus.SCHEDULED, TODAY, False, False),
    ],
    ids=[
        "scheduled_today",
        "scheduled_yesterday",
        "scheduled_on_launch_date",
        "draft",
        "in_review",
        "approved_not_scheduled",
        "future_date",
        "before_launch_date",
        "disabled_game",
    ],
)
async def test__visible_puzzle__only_when_scheduled_started_and_enabled(
    db_session: AsyncSession,
    status: PuzzleStatus,
    puzzle_date: dt.date,
    is_enabled: bool,
    is_visible: bool,
) -> None:
    game = await create_game(db_session, launch_date=LAUNCH, is_enabled=is_enabled)
    (answer,) = await create_titles(db_session, "Half-Life 2")
    puzzle = await create_puzzle(
        db_session, game=game, answer=answer, puzzle_date=puzzle_date, status=status
    )

    found = await get_visible_puzzle_with_details(
        db_session,
        game_id=game.id,
        variant=game.default_variant,
        puzzle_date=puzzle_date,
        today=TODAY,
    )

    assert (found is not None) is is_visible
    if found is not None:
        assert found.id == puzzle.id


async def test__delete_title__refused_while_a_puzzle_answer(db_session: AsyncSession) -> None:
    game = await create_game(db_session)
    (answer,) = await create_titles(db_session, "Half-Life 2")
    await create_puzzle(db_session, game=game, answer=answer, puzzle_date=TODAY)

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(delete(Title).where(Title.id == answer.id))


async def test__visible_puzzle_condition__needs_no_join_to_the_game(
    db_session: AsyncSession,
) -> None:
    enabled = await create_game(db_session, slug="enabled-game", launch_date=LAUNCH)
    disabled = await create_game(
        db_session, slug="disabled-game", launch_date=LAUNCH, is_enabled=False
    )
    (answer,) = await create_titles(db_session, "Half-Life 2")
    visible = await create_puzzle(db_session, game=enabled, answer=answer, puzzle_date=TODAY)
    await create_puzzle(db_session, game=disabled, answer=answer, puzzle_date=TODAY)

    result = await db_session.execute(select(Puzzle).where(visible_puzzle_condition(TODAY)))

    assert [puzzle.id for puzzle in result.scalars()] == [visible.id]


async def test__update_puzzle__updated_at_is_readable_after_flush(
    db_session: AsyncSession,
) -> None:
    game = await create_game(db_session)
    (answer,) = await create_titles(db_session, "Half-Life 2")
    puzzle = await create_puzzle(
        db_session, game=game, answer=answer, puzzle_date=TODAY, status=PuzzleStatus.DRAFT
    )

    puzzle.status = PuzzleStatus.APPROVED
    await db_session.flush()

    assert puzzle.updated_at is not None


async def test__visible_puzzle_condition__works_when_the_query_joins_the_game(
    db_session: AsyncSession,
) -> None:
    enabled = await create_game(db_session, slug="enabled-game", launch_date=LAUNCH)
    disabled = await create_game(
        db_session, slug="disabled-game", launch_date=LAUNCH, is_enabled=False
    )
    (answer,) = await create_titles(db_session, "Half-Life 2")
    visible = await create_puzzle(db_session, game=enabled, answer=answer, puzzle_date=TODAY)
    await create_puzzle(db_session, game=disabled, answer=answer, puzzle_date=TODAY)

    result = await db_session.execute(
        select(Puzzle)
        .join(Game, Game.id == Puzzle.game_id)
        .where(Game.slug == "enabled-game", visible_puzzle_condition(TODAY))
    )
    disabled_result = await db_session.execute(
        select(Puzzle)
        .join(Game, Game.id == Puzzle.game_id)
        .where(Game.slug == "disabled-game", visible_puzzle_condition(TODAY))
    )

    assert [puzzle.id for puzzle in result.scalars()] == [visible.id]
    assert list(disabled_result.scalars()) == []
