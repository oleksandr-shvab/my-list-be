import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.puzzle import visible_puzzle_condition
from app.models.puzzle import ClueMediaFile, Puzzle, PuzzleClue


async def get_visible_media_file(
    db: AsyncSession, *, key: str, today: dt.date
) -> ClueMediaFile | None:
    """The key is not unique (a content hash can repeat across clues) but the bytes are the same;
    take the first by id so the answer is stable (AC-12, R-19)."""
    result = await db.execute(
        select(ClueMediaFile)
        .join(PuzzleClue, ClueMediaFile.clue_id == PuzzleClue.id)
        .join(Puzzle, PuzzleClue.puzzle_id == Puzzle.id)
        .where(ClueMediaFile.key == key, visible_puzzle_condition(today))
        .order_by(ClueMediaFile.id)
        .limit(1)
    )
    return result.scalar_one_or_none()
