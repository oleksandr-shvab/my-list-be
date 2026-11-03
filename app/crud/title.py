from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.games.normalize import normalize_text
from app.models.title import Title, TitleKind


def _title_fields(name: str, franchise: str | None) -> dict[str, str | None]:
    """The stored name and franchise with their normalized copies. A blank franchise is stored as
    NULL, and so is a franchise that normalizes to nothing, so "a missing franchise never
    matches" (AC-28) holds. A name that normalizes to nothing is refused."""
    normalized_name = normalize_text(name)
    if not normalized_name:
        raise ValueError("A title name needs at least one letter or digit")
    normalized_franchise = normalize_text(franchise) if franchise is not None else ""
    return {
        "name": name,
        "normalized_name": normalized_name,
        "franchise": franchise if franchise is not None and franchise.strip() else None,
        "normalized_franchise": normalized_franchise or None,
    }


async def create_title(
    db: AsyncSession,
    *,
    name: str,
    franchise: str | None = None,
    year: int | None = None,
    kind: TitleKind = TitleKind.VIDEO_GAME,
) -> Title:
    """With `update_title`, the only place that writes `normalized_name` and
    `normalized_franchise`."""
    title = Title(kind=kind, year=year, **_title_fields(name, franchise))
    db.add(title)
    await db.flush()
    return title


async def update_title(
    db: AsyncSession, title: Title, *, name: str, franchise: str | None, year: int | None
) -> Title:
    """Change a title's name, franchise and year, and rewrite the normalized columns."""
    for field, value in _title_fields(name, franchise).items():
        setattr(title, field, value)
    title.year = year
    await db.flush()
    return title


async def find_title_by_name(db: AsyncSession, *, name: str, kind: TitleKind) -> Title | None:
    """The title of this kind with exactly this name; the lowest id if there are several."""
    result = await db.execute(
        select(Title).where(Title.kind == kind, Title.name == name).order_by(Title.id).limit(1)
    )
    return result.scalar_one_or_none()
