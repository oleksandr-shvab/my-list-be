from sqlalchemy.ext.asyncio import AsyncSession

from app.games.normalize import normalize_text
from app.models.title import Title, TitleKind


async def create_title(
    db: AsyncSession,
    *,
    name: str,
    franchise: str | None = None,
    year: int | None = None,
    kind: TitleKind = TitleKind.VIDEO_GAME,
) -> Title:
    """The only place that writes `normalized_name` and `normalized_franchise`.

    A blank franchise is stored as NULL, and so is a franchise that normalizes to nothing, so
    "a missing franchise never matches" (AC-28) holds. A name that normalizes to nothing is
    refused."""
    normalized_name = normalize_text(name)
    if not normalized_name:
        raise ValueError("A title name needs at least one letter or digit")
    normalized_franchise = normalize_text(franchise) if franchise is not None else ""
    title = Title(
        kind=kind,
        name=name,
        normalized_name=normalized_name,
        franchise=franchise if franchise is not None and franchise.strip() else None,
        normalized_franchise=normalized_franchise or None,
        year=year,
    )
    db.add(title)
    await db.flush()
    return title
