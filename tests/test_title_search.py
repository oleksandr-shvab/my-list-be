import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from app.core.exceptions import GameNotFoundError
from app.crud.title import SEARCH_LIMIT, create_title, search_titles
from app.games import service
from tests.factories import create_game

SETTING = "pg_trgm.word_similarity_threshold"
ZELDA = "The Legend of Zelda: Breath of the Wild"
CATALOGUE = (
    (ZELDA, "The Legend of Zelda", 2017),
    ("Half-Life 2", "Half-Life", 2004),
    ("Super Mario Bros.", "Mario", 1985),
    ("Mario", "Mario", 1983),
    ("Portal 2", "Portal", 2011),
    ("Hades", None, 2020),
    ("Doom", "Doom", 1993),
)


async def add_titles(db: AsyncSession, *titles: str | tuple[str, str]) -> None:
    """Each title is a name, or a (name, franchise) pair."""
    await create_game(db)
    for title in titles:
        name, franchise = (title, None) if isinstance(title, str) else title
        await create_title(db, name=name, franchise=franchise)


@pytest.fixture
async def catalogue(db_session: AsyncSession) -> None:
    await create_game(db_session)
    for name, franchise, year in CATALOGUE:
        await create_title(db_session, name=name, franchise=franchise, year=year)


async def search(db: AsyncSession, q: str) -> list[str]:
    result = await service.search_titles(db, slug="game-guesser", query=q)
    return [item.name for item in result.items]


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("Half-Life", "half life"),
        ("  Pokémon  ", "pokemon"),
        ("ab", "ab"),
        ("zl", "zl"),
        ("a", None),
        ("a.", None),
        ("a-", None),
        ("e\u0301", None),
        ("é", None),
        # One character that normalizes to two ("ss"): the trimmed length is what counts.
        ("ß", None),
        (" a ", None),
        ("", None),
        ("%", None),
        ("%%", None),
        ("\\\\", None),
        ("_ _", None),
    ],
)
def test__search_query__short_or_empty_is_none(q: str, expected: str | None) -> None:
    assert service.search_query(q) == expected


@pytest.mark.usefixtures("catalogue")
@pytest.mark.parametrize(
    ("q", "expected_first"),
    [
        ("zlda", ZELDA),
        ("half life", "Half-Life 2"),
        ("HALF-LIFE", "Half-Life 2"),
        ("  portal  ", "Portal 2"),
        ("the legend of zelda", ZELDA),
        ("hálf lífe", "Half-Life 2"),
    ],
)
async def test__search_titles__finds_typos_case_accents_and_franchises(
    db_session: AsyncSession, q: str, expected_first: str
) -> None:
    assert (await search(db_session, q))[0] == expected_first


@pytest.mark.usefixtures("catalogue")
async def test__search_titles__title_and_franchise_match_gives_one_row(
    db_session: AsyncSession,
) -> None:
    assert await search(db_session, "doom") == ["Doom"]


@pytest.mark.parametrize(
    ("names", "q", "expected"),
    [
        # The AC-21 row. It does not isolate a rule: "Mario" also wins by name.
        (("Super Mario Bros.", "Mario"), "mario", ["Mario", "Super Mario Bros."]),
        # The next rows tie on similarity, so without the rule the order falls to the name, which
        # the query sorts with COLLATE "C" (code point order) on every Postgres build. Exact
        # beats prefix: "Pokemon Go" sorts before "Pokémon" in that order.
        (("Pokemon Go", "Pokémon"), "pokemon", ["Pokémon", "Pokemon Go"]),
        # Prefix beats non-prefix: "A Portal Story" sorts before "Portal" and "Portal 2".
        (
            ("A Portal Story", "Portal 2", "Portal"),
            "portal",
            ["Portal", "Portal 2", "A Portal Story"],
        ),
        (("A Portal Story", "Portal 2"), "portal", ["Portal 2", "A Portal Story"]),
        # Neither is exact or a prefix. "Portel Quest" sorts first by name but is less similar
        # (0.57) than "Zed Portal" (1.0).
        (("Portel Quest", "Zed Portal"), "portal", ["Zed Portal", "Portel Quest"]),
        # "Zulu" matches by its franchise only, and the franchise similarity (1.0) outranks the
        # name similarity of "Alpha Psyonex" (0.63), though "Alpha Psyonex" sorts first by name.
        (("Alpha Psyonex", ("Zulu", "Psyonix")), "psyonix", ["Zulu", "Alpha Psyonex"]),
    ],
    ids=[
        "mario",
        "exact-before-prefix",
        "prefix-before-other",
        "prefix-before-other-no-exact",
        "similarity-before-name",
        "franchise-match-and-similarity",
    ],
)
async def test__search_titles__ranks_exact_then_prefix_then_similarity_then_name(
    db_session: AsyncSession,
    names: tuple[str | tuple[str, str], ...],
    q: str,
    expected: list[str],
) -> None:
    await add_titles(db_session, *names)

    assert await search(db_session, q) == expected


async def test__search_titles__sorts_ties_by_name_with_collate_c(db_session: AsyncSession) -> None:
    """The name tie-break must not depend on the database's default collation (ICU and glibc order
    "Pokémon" before "Pokemon Go", code points do not), so the SQL must say `COLLATE "C"`."""
    await add_titles(db_session, "Pokemon Go", "Pokémon")
    connection = db_session.bind
    assert isinstance(connection, AsyncConnection)
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(connection.sync_engine, "before_cursor_execute", record)
    try:
        await search(db_session, "pokemon")
    finally:
        event.remove(connection.sync_engine, "before_cursor_execute", record)

    [search_statement] = [text for text in statements if "word_similarity" in text.lower()]
    assert "ORDER BY" in search_statement
    assert 'titles.name COLLATE "C"' in search_statement


async def test__search_titles__threshold_does_not_outlive_the_transaction(
    engine: AsyncEngine,
) -> None:
    """`set_config(..., true)` is local to the transaction. With `false` a committed search would
    leave 0.4 on the pooled connection for every later query."""
    async with engine.connect() as connection, AsyncSession(bind=connection) as session:
        await search_titles(session, kind="video_game", normalized_query="zelda")
        in_search = await session.scalar(text("SELECT current_setting(:name)"), {"name": SETTING})
        await session.commit()
        after_commit = await session.scalar(
            text("SELECT current_setting(:name)"), {"name": SETTING}
        )

    assert (in_search, after_commit) == ("0.4", "0.6")


@pytest.mark.usefixtures("catalogue")
async def test__search_titles__only_finds_titles_of_the_games_kind(
    db_session: AsyncSession,
) -> None:
    """The `kind` column only allows `video_game` for now, so a title of another kind cannot be
    stored; asking for another kind must find nothing."""
    assert await search_titles(db_session, kind="board_game", normalized_query="zelda") == []


@pytest.mark.usefixtures("catalogue")
@pytest.mark.parametrize(
    "q",
    [
        "qzxkjv",
        "wwwwwwww",
        "zzzzzz yyyyyy",
        "a" * 100,
        "",
        " ",
        "a",
        " a ",
        "%",
        "_",
        "\\",
        "%%",
        "__",
        "\\\\",
        "_%_",
    ],
)
async def test__search_titles__nonsense_short_and_symbol_only_queries_are_empty(
    db_session: AsyncSession, q: str
) -> None:
    assert await search(db_session, q) == []


async def test__search_titles__returns_at_most_200(db_session: AsyncSession) -> None:
    await add_titles(db_session, *(f"Quest {number:03d}" for number in range(250)))

    names = await search(db_session, "quest")

    assert len(names) == SEARCH_LIMIT == 200
    # Ties are broken by name, so the cut is stable.
    assert names == sorted(names)
    assert names[0] == "Quest 000"


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        # `LIKE 'a_pha'` would match "Alpha": `_` is read as a space, not as any one character.
        ("a_pha", []),
        # `%` is dropped by normalization, so it neither widens the search nor matches itself.
        ("100%", ["100% Orange Juice"]),
        ("%", []),
        ("100%%", ["100% Orange Juice"]),
        ("a\\pha", []),
        ("\\", []),
    ],
)
async def test__search_titles__percent_underscore_and_backslash_are_ignored(
    db_session: AsyncSession, q: str, expected: list[str]
) -> None:
    """Normalization turns `%`, `_` and backslashes into spaces, so they never reach the database
    as wildcards (AC-22)."""
    await add_titles(db_session, "100% Orange Juice", "Alpha", "Beta")

    assert await search(db_session, q) == expected


@pytest.mark.parametrize(
    ("slug", "q"),
    [("no-such-game", "zelda"), ("off-game", "zelda"), ("no-such-game", "a"), ("off-game", "")],
)
async def test__search_titles__unknown_or_disabled_game_is_not_found(
    db_session: AsyncSession, slug: str, q: str
) -> None:
    await create_game(db_session, slug="off-game", is_enabled=False)

    with pytest.raises(GameNotFoundError):
        await service.search_titles(db_session, slug=slug, query=q)


@pytest.mark.usefixtures("catalogue")
async def test__search_titles__route_returns_fields_and_public_cache(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/games/game-guesser/titles", params={"q": "zelda"})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    [item] = response.json()["items"]
    # Exactly these keys: nothing says whether a title is an answer (AC-19).
    assert set(item) == {"id", "name", "franchise", "year"}
    assert (item["name"], item["franchise"], item["year"]) == (ZELDA, "The Legend of Zelda", 2017)
    assert isinstance(item["id"], int)


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"q": "a" * 101}, {"field": "q", "code": "too_long", "params": {"max": 100}}),
        ({}, {"field": "q", "code": "required", "params": {}}),
    ],
    ids=["over-100-characters", "missing"],
)
async def test__search_titles__route_validation_is_422(
    client: AsyncClient, db_session: AsyncSession, params: dict[str, str], error: dict[str, object]
) -> None:
    """Only FastAPI enforces `q`: required, at most 100 characters (AC-20)."""
    await create_game(db_session)

    response = await client.get("/v1/games/game-guesser/titles", params=params)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [error]
