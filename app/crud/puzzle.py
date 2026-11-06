import datetime as dt
import uuid
from collections.abc import Collection
from typing import NamedTuple

from sqlalchemy import ColumnElement, and_, exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.game import Game
from app.models.progress import Progress, ProgressGuess
from app.models.puzzle import Puzzle, PuzzleClue, PuzzleStatus


def visible_puzzle_condition(today: dt.date) -> ColumnElement[bool]:
    """The only visibility rule for puzzles (AC-12). Every puzzle query takes it; do not copy it
    into Python.

    The game terms are a correlated `EXISTS`, so it works in any `select(Puzzle)` with no join to
    `Game`. A later admin preview (Q12) widens the rule here and nowhere else.
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
    result = await db.execute(
        select(Puzzle)
        .where(
            Puzzle.game_id == game_id,
            Puzzle.variant == variant,
            Puzzle.date == puzzle_date,
            visible_puzzle_condition(today),
        )
        .options(
            joinedload(Puzzle.answer_title),
            joinedload(Puzzle.clues).joinedload(PuzzleClue.media_files),
        )
    )
    return result.unique().scalar_one_or_none()


class ArchiveRow(NamedTuple):
    """Only an account's query sets `status` and `guesses_used`; a guest's returns the date alone.
    `status` is None when the account never started the puzzle."""

    date: dt.date
    status: str | None = None
    guesses_used: int | None = None


async def list_archive_puzzles(
    db: AsyncSession,
    *,
    game_id: int,
    variant: str,
    before: dt.date,
    today: dt.date,
    limit: int,
    user_id: uuid.UUID | None,
) -> list[ArchiveRow]:
    """A keyset page (AC-63): the caller asks for `limit + 1` to learn whether a next page exists.
    Days with no visible puzzle are absent (AC-65). Nothing of the progress but the status and
    the guess count leaves this query.

    `before` must not be after `today`: the caller clamps it, and `visible_puzzle_condition` is a
    second guard."""
    where = (
        Puzzle.game_id == game_id,
        Puzzle.variant == variant,
        Puzzle.date < before,
        visible_puzzle_condition(today),
    )
    if user_id is None:
        query = select(Puzzle.date).where(*where)
    else:
        guesses_used = (
            select(func.count(ProgressGuess.id))
            .where(ProgressGuess.progress_id == Progress.id)
            .scalar_subquery()
        )
        query = (
            select(Puzzle.date, Progress.status, guesses_used)
            .outerjoin(Progress, and_(Progress.puzzle_id == Puzzle.id, Progress.user_id == user_id))
            .where(*where)
        )
    result = await db.execute(query.order_by(Puzzle.date.desc()).limit(limit))
    return [ArchiveRow(*row) for row in result.all()]


async def list_visible_puzzles(
    db: AsyncSession, *, game_id: int, keys: Collection[tuple[str, dt.date]], today: dt.date
) -> list[Puzzle]:
    """Loads the answer title only, no clues or media: enough to replay guesses, never enough to
    show a view."""
    if not keys:
        return []
    result = await db.execute(
        select(Puzzle)
        .where(
            Puzzle.game_id == game_id,
            tuple_(Puzzle.variant, Puzzle.date).in_(list(keys)),
            visible_puzzle_condition(today),
        )
        .options(joinedload(Puzzle.answer_title))
    )
    return list(result.scalars())
