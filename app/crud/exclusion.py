from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exclusion import AnswerExclusion
from app.models.title import Title


class ExclusionRow(NamedTuple):
    """One exclusion with its title's IGDB id (`None` for a title that did not come from IGDB)."""

    title_id: int
    igdb_id: int | None
    reason: str
    variant: str
    build_slot: int | None
    image_ids: list[str]


async def list_exclusions(db: AsyncSession, game_id: int) -> list[ExclusionRow]:
    """Every exclusion of the game, oldest first. Rows can repeat for one title and slot: a title
    rejected twice, or rebuilt and then rejected."""
    result = await db.execute(
        select(
            AnswerExclusion.title_id,
            Title.igdb_id,
            AnswerExclusion.reason,
            AnswerExclusion.variant,
            AnswerExclusion.build_slot,
            AnswerExclusion.image_ids,
        )
        .join(Title, Title.id == AnswerExclusion.title_id)
        .where(AnswerExclusion.game_id == game_id)
        .order_by(AnswerExclusion.id)
    )
    return [ExclusionRow(*row) for row in result.all()]
