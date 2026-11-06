from collections.abc import Collection, Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import set_local
from app.games.normalize import normalize_text
from app.models.title import Title, TitleKind


def _title_fields(name: str, franchise: str | None) -> dict[str, str | None]:
    """A blank franchise, or one that normalizes to nothing, is stored as NULL, so a missing
    franchise never matches (AC-28). A name that normalizes to nothing is refused."""
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
    for field, value in _title_fields(name, franchise).items():
        setattr(title, field, value)
    title.year = year
    await db.flush()
    return title


async def find_title_by_name(db: AsyncSession, *, name: str, kind: TitleKind) -> Title | None:
    """The lowest id if several titles share the name."""
    result = await db.execute(
        select(Title).where(Title.kind == kind, Title.name == name).order_by(Title.id).limit(1)
    )
    return result.scalar_one_or_none()


# Title keys are 32-bit integers; asyncpg refuses a larger bound value, which would be a 500.
MAX_TITLE_ID = 2**31 - 1


async def get_titles_by_ids(
    db: AsyncSession, *, ids: Collection[int], kind: TitleKind
) -> dict[int, Title]:
    keys = {title_id for title_id in ids if 1 <= title_id <= MAX_TITLE_ID}
    if not keys:
        return {}
    result = await db.execute(select(Title).where(Title.kind == kind, Title.id.in_(keys)))
    return {title.id: title for title in result.scalars()}


SEARCH_LIMIT = 200
# Word similarity (`pg_trgm`) a title must reach to match. The default 0.6 misses "zlda" against
# "the legend of zelda breath of the wild"; 0.4 finds it (SPEC-02 research note).
SEARCH_SIMILARITY_THRESHOLD = 0.4


async def search_titles(db: AsyncSession, *, kind: str, normalized_query: str) -> Sequence[Title]:
    """`normalized_query` is `normalize_text` output and not empty. The trigram operators and
    `starts_with` take no wildcards and the text is a bound parameter, so the query is plain text
    (AC-22)."""
    await set_local(db, "pg_trgm.word_similarity_threshold", str(SEARCH_SIMILARITY_THRESHOLD))
    # `%>` is served by the GIN index.
    name_matches = Title.normalized_name.op("%>")(normalized_query)
    franchise_matches = Title.normalized_franchise.op("%>")(normalized_query)
    similarity = func.greatest(
        func.word_similarity(normalized_query, Title.normalized_name),
        func.word_similarity(normalized_query, Title.normalized_franchise),
    )
    result = await db.execute(
        select(Title)
        .where(Title.kind == kind, name_matches | franchise_matches)
        .order_by(
            desc(Title.normalized_name == normalized_query),
            desc(func.starts_with(Title.normalized_name, normalized_query)),
            desc(similarity),
            # `COLLATE "C"`: code point order, whatever the database's default collation is, so
            # ties and the cut at 200 are the same on every Postgres build.
            Title.name.collate("C"),
            Title.id,
        )
        .limit(SEARCH_LIMIT)
    )
    return result.scalars().all()
