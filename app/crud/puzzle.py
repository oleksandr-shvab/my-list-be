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
    """The visible puzzle with its answer title, its clues (in step order) and their media files,
    in one query."""
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
    """One past puzzle. `status` and `guesses_used` are read only when the query was made for an
    account; for a guest the query returns the date alone. For an account, `status` is None when it
    never started the puzzle."""

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
    """Visible puzzles dated strictly before `before`, newest first: a keyset page (AC-63). The
    caller asks for `limit + 1` to learn whether there is a next page. Days with no visible puzzle
    are simply absent (AC-65). With a `user_id`, the account's own progress is left-joined for the
    status and the number of stored guesses; nothing else of the progress leaves this query. Without
    one, only the dates are read, and the service decides what a guest sees.

    `before` must not be after `today`: the caller clamps it, and `visible_puzzle_condition` is a
    second guard, so today and the future can never appear."""
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
    """The visible puzzles of a game for these `(variant, date)` pairs, each with its answer
    title and nothing else (no clues, no media), in one query. A pair with no visible puzzle has
    no row. Enough to replay guesses; never enough to show a view."""
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
