import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.title import create_title
from app.models.title import Title


async def test__create_title__sets_both_normalized_columns(db_session: AsyncSession) -> None:
    title = await create_title(db_session, name="Half-Life 2", franchise="Pokémon", year=2004)

    assert title.id is not None
    assert title.normalized_name == "half life 2"
    assert title.normalized_franchise == "pokemon"
    assert (title.name, title.franchise, title.year) == ("Half-Life 2", "Pokémon", 2004)
    assert title.kind == "video_game"


async def test__create_title__null_franchise_leaves_normalized_franchise_null(
    db_session: AsyncSession,
) -> None:
    title = await create_title(db_session, name="Portal")

    assert title.franchise is None
    assert title.normalized_franchise is None
    assert title.year is None


@pytest.mark.parametrize(
    ("franchise", "stored_franchise"),
    [("", None), ("   ", None), ("!!!", "!!!")],
    ids=["empty", "spaces_only", "punctuation_only"],
)
async def test__create_title__blank_franchise_is_stored_as_null(
    db_session: AsyncSession, franchise: str, stored_franchise: str | None
) -> None:
    title = await create_title(db_session, name="Portal", franchise=franchise)

    assert title.franchise == stored_franchise
    assert title.normalized_franchise is None


@pytest.mark.parametrize(
    "name", ["", "   ", "!!!"], ids=["empty", "spaces_only", "punctuation_only"]
)
async def test__create_title__name_with_nothing_to_match_is_refused(
    db_session: AsyncSession, name: str
) -> None:
    with pytest.raises(ValueError, match="letter or digit"):
        await create_title(db_session, name=name)


async def test__title_normalized_name__empty_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    title = Title(kind="video_game", name="!!!", normalized_name="")

    with pytest.raises(IntegrityError, match="ck_titles_normalized_name_not_empty"):
        async with db_session.begin_nested():
            db_session.add(title)
            await db_session.flush()
