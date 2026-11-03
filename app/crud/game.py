from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.game import Game


async def list_enabled_games(db: AsyncSession) -> Sequence[Game]:
    result = await db.execute(select(Game).where(Game.is_enabled.is_(True)).order_by(Game.slug))
    return result.scalars().all()


async def get_enabled_game_by_slug(db: AsyncSession, slug: str) -> Game | None:
    result = await db.execute(select(Game).where(Game.slug == slug, Game.is_enabled.is_(True)))
    return result.scalar_one_or_none()
