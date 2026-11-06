import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.puzzle import visible_puzzle_condition
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue


async def get_visible_media_file(
    db: AsyncSession, *, key: str, today: dt.date
) -> ClueMediaFile | None:
    """A file with this key that belongs to a visible puzzle (AC-12, R-19). The key is not unique
    (a content hash can repeat across clues), so any visible file with it will do: the bytes are
    the same, and the first by id is taken so the answer does not change between requests."""
    result = await db.execute(
        select(ClueMediaFile)
        .join(PuzzleClue, ClueMediaFile.clue_id == PuzzleClue.id)
        .join(Puzzle, PuzzleClue.puzzle_id == Puzzle.id)
        .where(ClueMediaFile.key == key, visible_puzzle_condition(today))
        .order_by(ClueMediaFile.id)
        .limit(1)
    )
    return result.scalar_one_or_none()
