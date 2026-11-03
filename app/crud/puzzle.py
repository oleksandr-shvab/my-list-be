import datetime as dt

from sqlalchemy import ColumnElement, and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.game import Game
from app.models.puzzle import Puzzle, PuzzleStatus


def visible_puzzle_condition(today: dt.date) -> ColumnElement[bool]:
    """The only visibility rule for puzzles (AC-12). Every puzzle query takes it: view, today,
    resume, guess, archive, merge and the media lookup. Do not copy it into Python.

    A puzzle is visible only while it is `scheduled`, its date is today or earlier, its date is
    on or after the game's launch date, and its game is enabled. The game terms are a correlated
    `EXISTS`, so the condition works in any `select(Puzzle)` with no join to `Game`.

    A later admin preview (decision Q12) widens the rule here, and nowhere else.
    """
    return and_(
        Puzzle.status == PuzzleStatus.SCHEDULED,
        Puzzle.date <= today,
        exists(
            select(Game.id)
            .where(
                Game.id == Puzzle.game_id,
                Game.is_enabled.is_(True),
                Puzzle.date >= Game.launch_date,
            )
            # Correlate only to `puzzles`, so the condition also works when the outer query
            # already joins `games`.
            .correlate(Puzzle)
        ),
    )


async def get_visible_puzzle_with_details(
    db: AsyncSession, *, game_id: int, variant: str, puzzle_date: dt.date, today: dt.date
) -> Puzzle | None:
    """The visible puzzle with its answer title and its clues (in step order), in one query."""
    result = await db.execute(
        select(Puzzle)
        .where(
            Puzzle.game_id == game_id,
            Puzzle.variant == variant,
            Puzzle.date == puzzle_date,
            visible_puzzle_condition(today),
        )
        .options(joinedload(Puzzle.answer_title), joinedload(Puzzle.clues))
    )
    return result.unique().scalar_one_or_none()
