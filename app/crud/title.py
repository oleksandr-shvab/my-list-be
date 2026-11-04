from collections.abc import Sequence

from sqlalchemy import desc, func, select
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


SEARCH_LIMIT = 200
# Word similarity (`pg_trgm`) a title must reach to match. The default 0.6 misses "zlda" against
# "the legend of zelda breath of the wild"; 0.4 finds it (SPEC-02 research note).
SEARCH_SIMILARITY_THRESHOLD = 0.4


async def search_titles(db: AsyncSession, *, kind: str, normalized_query: str) -> Sequence[Title]:
    """At most `SEARCH_LIMIT` titles of this kind whose normalized name or franchise is close to
    `normalized_query` (the output of `normalize_text`, not empty).

    Ranked by an exact name match, then a name that starts with the query, then the higher
    word similarity of the name and the franchise, then name (code point order) and id. The trigram operators and
    `starts_with` take no wildcards, and the text is a bound parameter, so the query is plain
    text (AC-22). The threshold is set for the current transaction only."""
    await db.execute(
        select(
            func.set_config(
                "pg_trgm.word_similarity_threshold", str(SEARCH_SIMILARITY_THRESHOLD), True
            )
        )
    )
    # `%>`: the query is similar to some run of words in the column; the GIN index serves it.
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
