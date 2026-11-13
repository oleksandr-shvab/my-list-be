"""The title import and the target guard (M1-21, SPEC-04 AC-10 to AC-19, R-13, R-15).

No network: the IGDB client reads `tests/fixtures/igdb/games_import.json` over
`httpx.MockTransport`. The guard is checked with an environment mapping of the test's own."""

import datetime as dt
import json
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.content import titles_import
from app.content.franchise_overrides import FranchiseSource
from app.content.igdb import DropReason, IgdbClient, IgdbGame, SkipReason, parse_game
from app.content.target import TargetRefusedError, check_target
from app.content.titles_import import (
    IGDB_WHERE,
    ImportRecord,
    ImportReport,
    NameProblem,
    franchise_of,
    in_scope,
    select_records,
    to_record,
    upsert_titles,
)
from app.crud.title import create_title
from app.models.progress import Progress, ProgressGuess, ProgressOrigin
from app.models.puzzle import Puzzle
from app.models.title import Title
from app.models.user import User
from tests.factories import create_game, create_puzzle

FIXTURE = Path(__file__).parent / "fixtures" / "igdb" / "games_import.json"
NOW = dt.datetime(2026, 11, 12, tzinfo=dt.UTC)


def make_game(**fields: Any) -> IgdbGame:
    raw = {
        "id": 1,
        "name": "Example",
        "slug": "example",
        "game_type": 0,
        "total_rating_count": 5,
        **fields,
    }
    game = parse_game(raw, max_year=2027, skips=Counter(), drops=Counter())
    assert game is not None
    return game


def make_record(igdb_id: int, name: str, **fields: Any) -> ImportRecord:
    game = make_game(id=igdb_id, name=name, **fields)
    record = to_record(game)
    assert isinstance(record, ImportRecord)
    return record


async def read_fixture() -> ImportReport:
    """A whole run over the fake IGDB, up to the upsert, like `scripts/import_titles.py`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "fixture-token"})
        assert request.url.host == "api.igdb.com"
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

    async with IgdbClient(
        "fixture-id", SecretStr("fixture-secret"), transport=httpx.MockTransport(handler)
    ) as igdb:
        selection = await select_records(igdb.games(now=NOW, where=IGDB_WHERE))
        return ImportReport(
            selection, titles_import.UpsertCounts(), skipped=igdb.skipped, dropped=igdb.dropped
        )


# --- the target guard (R-13) ---------------------------------------------------------------------

LOCAL_SETTINGS = SimpleNamespace(
    DATABASE_URL="postgresql+asyncpg://user:hunter2@localhost:5544/db",
    MEDIA_STORAGE="local",
    S3_BUCKET=None,
    media_local_path=Path("/media"),
)
REMOTE_SETTINGS = SimpleNamespace(
    DATABASE_URL="postgresql+asyncpg://user:hunter2@db.example.com:5432/db",
    MEDIA_STORAGE="s3",
    S3_BUCKET="the-bucket",
    media_local_path=Path("/media"),
)


@pytest.mark.parametrize(
    ("target", "confirm", "environ", "app_settings", "typed", "is_refused"),
    [
        ("local", None, {"ENVIRONMENT": "local"}, LOCAL_SETTINGS, None, False),
        ("production", "production", {"ENVIRONMENT": "production"}, REMOTE_SETTINGS, None, False),
        ("production", None, {"ENVIRONMENT": "production"}, REMOTE_SETTINGS, "production", False),
        ("local", None, {"ENVIRONMENT": "local", "CI": "true"}, LOCAL_SETTINGS, None, True),
        ("local", None, {"ENVIRONMENT": "local", "CI": "1"}, LOCAL_SETTINGS, None, True),
        ("local", None, {"ENVIRONMENT": "local", "CI": "false"}, LOCAL_SETTINGS, None, False),
        ("local", None, {"ENVIRONMENT": "local", "CI": "0"}, LOCAL_SETTINGS, None, False),
        ("local", None, {"ENVIRONMENT": "local", "CI": " No "}, LOCAL_SETTINGS, None, False),
        ("local", None, {"ENVIRONMENT": "local", "CI": ""}, LOCAL_SETTINGS, None, False),
        (None, None, {"ENVIRONMENT": "local"}, LOCAL_SETTINGS, None, True),
        ("production", "production", {"ENVIRONMENT": "local"}, REMOTE_SETTINGS, None, True),
        ("local", None, {}, LOCAL_SETTINGS, None, True),
        ("production", None, {"ENVIRONMENT": "production"}, REMOTE_SETTINGS, None, True),
        ("production", "prod", {"ENVIRONMENT": "production"}, REMOTE_SETTINGS, None, True),
        ("production", None, {"ENVIRONMENT": "production"}, REMOTE_SETTINGS, "yes", True),
        ("local", None, {"ENVIRONMENT": "local"}, REMOTE_SETTINGS, None, True),
    ],
    ids=[
        "local_ok",
        "remote_confirmed",
        "remote_typed",
        "ci",
        "ci_1",
        "ci_false",
        "ci_0",
        "ci_no",
        "ci_empty",
        "no_target",
        "target_differs_from_environment",
        "no_environment",
        "remote_unconfirmed",
        "remote_wrong_confirmation",
        "remote_wrong_typed_name",
        "local_with_remote_database",
    ],
)
def test__check_target__allows_or_refuses(
    target: str | None,
    confirm: str | None,
    environ: dict[str, str],
    app_settings: SimpleNamespace,
    typed: str | None,
    is_refused: bool,
) -> None:
    ask = (lambda _prompt: typed) if typed is not None else None

    def check() -> str:
        return check_target(
            target,
            confirm,
            environ=environ,
            app_settings=app_settings,
            ask=ask,
        ).describe()

    if is_refused:
        with pytest.raises(TargetRefusedError):
            check()
        return
    line = check()
    assert f"Target: {target}" in line
    assert app_settings.DATABASE_URL.split("@")[1].split(":")[0] in line
    assert "hunter2" not in line


# --- the mapping (AC-10 to AC-13) -----------------------------------------------------------------


ZELDA_OVERRIDE = {(FranchiseSource.COLLECTION, 3): "The Legend of Zelda"}


@pytest.mark.parametrize(
    ("fields", "overrides", "expected"),
    [
        (
            {"franchise": {"id": 1, "name": "Main"}, "collections": [{"id": 2, "name": "Series"}]},
            {},
            "Main",
        ),
        ({"collections": [{"id": 9, "name": "Zed"}, {"id": 3, "name": "Alpha"}]}, {}, "Alpha"),
        ({}, {}, None),
        ({"collections": [{"id": 3, "name": "  The   Legend  (series) "}]}, {}, "The Legend"),
        ({"collections": [{"id": 3, "name": "(series)"}, {"id": 4, "name": "Next"}]}, {}, "Next"),
        ({"collections": [{"id": 3, "name": "Zelda"}]}, ZELDA_OVERRIDE, "The Legend of Zelda"),
        ({"collections": [{"id": 3, "name": "N" * 256}]}, {}, None),
        # 60 characters that NFKD turns into 1,080: too long once normalized.
        (
            {"collections": [{"id": 3, "name": "\ufdfa" * 60}, {"id": 4, "name": "Fits"}]},
            {},
            "Fits",
        ),
    ],
    ids=[
        "main_franchise_first",
        "first_series_by_id",
        "neither",
        "qualifier_and_spaces_cleaned",
        "empty_after_cleaning_passes_to_next",
        "override_applied",
        "too_long_for_the_column",
        "too_long_once_normalized",
    ],
)
def test__franchise_of__follows_ac12(
    monkeypatch: pytest.MonkeyPatch,
    fields: dict[str, Any],
    overrides: dict[tuple[FranchiseSource, int], str],
    expected: str | None,
) -> None:
    monkeypatch.setattr(titles_import, "FRANCHISE_OVERRIDES", overrides)

    assert franchise_of(make_game(**fields)) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        *[({"game_type": kind}, True) for kind in (0, 4, 8, 9, 10)],
        *[({"game_type": kind}, False) for kind in (1, 2, 3, 5, 6, 7, 11, 12, 13, 14)],
        ({"game_type": None}, False),
        ({"version_parent": 99}, False),
        ({"total_rating_count": 0}, False),
        ({"total_rating_count": None}, False),
        ({"total_rating_count": 1}, True),
    ],
)
def test__in_scope__keeps_only_the_games_of_ac11(fields: dict[str, Any], expected: bool) -> None:
    assert in_scope(make_game(**fields)) is expected


@pytest.mark.parametrize(
    ("name", "released", "expected"),
    [
        ("N" * 255, None, ("n" * 255, None)),
        ("N" * 256, None, NameProblem.TOO_LONG),
        ("\ufdfa" * 60, None, NameProblem.UNUSABLE),
        ("Zelda: Ocarina of Time", 912470400, ("zelda ocarina of time", 1998)),
        ("Hades", -631152000, ("hades", 1950)),
    ],
    ids=[
        "longest_name",
        "name_too_long",
        "too_long_once_normalized",
        "year_from_timestamp",
        "year_1950",
    ],
)
def test__to_record__year_and_name_limit(
    name: str, released: int | None, expected: tuple[str, int | None] | NameProblem
) -> None:
    record = to_record(make_game(name=name, first_release_date=released))

    if isinstance(expected, NameProblem):
        assert record is expected
    else:
        assert isinstance(record, ImportRecord)
        assert (record.normalized_name, record.year) == expected


# --- the import into the database (AC-14 to AC-19) -----------------------------------------------


async def all_titles(db: AsyncSession) -> list[tuple[str, int | None, str | None]]:
    result = await db.execute(select(Title.name, Title.year, Title.franchise).order_by(Title.id))
    return [(name, year, franchise) for name, year, franchise in result]


async def test__import__fixture_run_keeps_one_doom_per_year_and_a_second_run_changes_nothing(
    db_session: AsyncSession,
) -> None:
    report = await read_fixture()
    first = await upsert_titles(db_session, report.selection.records)
    first_rows = await all_titles(db_session)
    second = await upsert_titles(db_session, (await read_fixture()).selection.records)

    # AC-14: "Doom" 1993 twice gives one title, "Doom" 2016 stays.
    assert first_rows == [
        ("Doom", 1993, "Doom"),
        ("Doom", 2016, "Doom"),
        ("Example Quest", 2001, "Example Series"),
        ("Example Mixed", 2003, None),
        ("Example Undated", None, None),
    ]
    assert (first.added, first.updated, first.unchanged) == (5, 0, 0)
    # AC-16: the same data again changes nothing.
    assert (second.added, second.updated, second.unchanged, second.refused) == (0, 0, 5, 0)
    assert await all_titles(db_session) == first_rows
    # AC-19: the counts, with the client's records skipped and items dropped kept apart.
    assert (first.near_duplicates, second.near_duplicates) == (1, 1)
    assert report.selection.out_of_scope == 3
    assert report.selection.name_problems == {NameProblem.TOO_LONG: 1}
    assert report.skipped == {SkipReason.NAME_WITHOUT_LETTERS: 1}
    assert report.dropped == {DropReason.GENRE_INVALID: 1}
    report.counts = first
    lines = "\n".join(report.lines())
    assert "records skipped as invalid (AC-8): 1" in lines
    assert "items dropped from records that were kept: 1" in lines


async def test__import__near_duplicates_are_counted_and_a_tie_in_ratings_goes_to_the_lower_id(
    db_session: AsyncSession,
) -> None:
    await create_title(db_session, name="Bar", year=2001, igdb_id=1)
    mid_2001 = 995000000
    records = [
        make_record(1, "Foo", first_release_date=mid_2001, total_rating_count=5),
        # A new game with more ratings loses the name to the stored title that takes it.
        make_record(4, "Foo", first_release_date=mid_2001, total_rating_count=900),
        # Two new games with the same ratings: the lower IGDB id is added, the other is left out.
        make_record(6, "QUUX", first_release_date=mid_2001, total_rating_count=7),
        make_record(5, "Quux", first_release_date=mid_2001, total_rating_count=7),
    ]

    counts = await upsert_titles(db_session, records)

    result = await db_session.execute(select(Title.igdb_id, Title.name).order_by(Title.igdb_id))
    assert result.tuples().all() == [(1, "Foo"), (5, "Quux")]
    assert (counts.added, counts.updated, counts.near_duplicates) == (1, 1, 2)


def test__report__lists_every_count_and_at_most_twenty_refused_ids() -> None:
    selection = titles_import.Selection(
        out_of_scope=2,
        name_problems=Counter({NameProblem.UNUSABLE: 1, NameProblem.TOO_LONG: 3}),
    )
    counts = titles_import.UpsertCounts(
        added=4, updated=3, unchanged=2, near_duplicates=7, refused=25
    )
    counts.refused_title_ids = list(range(1, 26))
    report = ImportReport(
        selection,
        counts,
        skipped=Counter({SkipReason.YEAR_OUT_OF_RANGE: 2, SkipReason.WRONG_SHAPE: 1}),
        dropped=Counter({DropReason.GENRE_INVALID: 1}),
    )

    lines = report.lines()

    assert lines == [
        "titles added: 4",
        "titles updated: 3",
        "titles unchanged: 2",
        "kept as near-duplicates (not added): 7",
        "refused to change, would break a stored game (AC-18): 25",
        "records skipped as invalid (AC-8): 3 (wrong_shape: 1, year_out_of_range: 2)",
        "items dropped from records that were kept: 1 (genre_invalid: 1)",
        "records out of scope: 2",
        "records with a name that does not fit the list: 4 (too long: 3, unusable: 1)",
        f"refused title ids: {', '.join(str(n) for n in range(1, 21))} and 5 more",
    ]
    # Exactly twenty fit: nothing more to say.
    counts.refused_title_ids = list(range(1, 21))
    assert report.lines()[-1] == f"refused title ids: {', '.join(str(n) for n in range(1, 21))}"


async def play_stored_game(db: AsyncSession, puzzle: Puzzle, *, guessed: list[Title]) -> None:
    """A stored game of somebody on `puzzle`, with these titles as its guesses (R-15)."""
    user = User(email="player@example.com", hashed_password="not-a-real-hash")
    db.add(user)
    await db.flush()
    progress = Progress(
        user_id=user.id,
        puzzle_id=puzzle.id,
        status="in_progress",
        started_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        counts_for_streak=True,
        origin=ProgressOrigin.PLAY,
    )
    db.add(progress)
    await db.flush()
    db.add_all(
        ProgressGuess(progress_id=progress.id, position=position, title_id=title.id)
        for position, title in enumerate(guessed, start=1)
    )
    await db.flush()


async def test__import__never_deletes_and_refuses_what_would_break_a_stored_game(
    db_session: AsyncSession,
) -> None:
    first = [
        make_record(1, "Guessed", franchise={"id": 1, "name": "Old"}),
        make_record(2, "Answer", franchise={"id": 1, "name": "Old"}),
        make_record(3, "Display"),
        make_record(4, "Planned"),
        make_record(5, "Free"),
        make_record(6, "Gone"),
    ]
    await upsert_titles(db_session, first)
    ids = {title.igdb_id: title for title in (await db_session.scalars(select(Title)))}
    game = await create_game(db_session)
    # "Answer" is the answer of a puzzle somebody has played; "Planned" of one nobody has.
    played = await create_puzzle(
        db_session, game=game, answer=ids[2], puzzle_date=dt.date(2026, 11, 1)
    )
    await create_puzzle(db_session, game=game, answer=ids[4], puzzle_date=dt.date(2026, 11, 2))
    await play_stored_game(db_session, played, guessed=[ids[1], ids[3]])

    second = [
        make_record(1, "Guessed Again", franchise={"id": 1, "name": "Old"}),  # renamed: refused
        make_record(2, "Answer", franchise={"id": 2, "name": "New"}),  # franchise: refused
        make_record(3, "DISPLAY"),  # same normalized name: allowed
        make_record(4, "Planned Two"),  # answer of a puzzle with no games: allowed
        make_record(5, "Free Two"),  # allowed
        # igdb 6 is missing from this run (AC-17).
        # A new game with the name "Guessed" meets the title that keeps it: a near-duplicate.
        make_record(7, "Guessed", franchise={"id": 1, "name": "Old"}),
        # A new game with the name that the refused title wanted: that title cannot take it, so
        # the name is free and the game is added.
        make_record(8, "Guessed Again", franchise={"id": 1, "name": "Old"}),
    ]
    counts = await upsert_titles(db_session, second)

    names = {title.igdb_id: title.name for title in (await db_session.scalars(select(Title)))}
    assert names == {
        1: "Guessed",
        2: "Answer",
        3: "DISPLAY",
        4: "Planned Two",
        5: "Free Two",
        6: "Gone",
        8: "Guessed Again",
    }
    assert ids[2].franchise == "Old"
    assert (counts.refused, counts.updated, counts.unchanged) == (2, 3, 0)
    assert (counts.added, counts.near_duplicates) == (1, 1)
    assert sorted(counts.refused_title_ids) == sorted([ids[1].id, ids[2].id])

    # A second run with the same data changes nothing.
    again = await upsert_titles(db_session, second)
    assert (again.added, again.updated, again.refused, again.near_duplicates) == (0, 0, 2, 1)


async def test__import__a_new_record_with_the_name_and_year_of_a_stored_title_is_not_added(
    db_session: AsyncSession,
) -> None:
    await create_title(db_session, name="Portal", year=2007)  # made by hand, no IGDB id
    records = [make_record(1, "PORTAL", first_release_date=1191974400)]  # 2007
    records.append(make_record(2, "Portal", first_release_date=1303430400))  # 2011

    counts = await upsert_titles(db_session, records)

    assert (counts.added, counts.near_duplicates) == (1, 1)
    count = await db_session.scalar(select(func.count()).select_from(Title))
    assert count == 2

    # An update onto the name and year of another stored title is a near-duplicate too.
    renamed = await upsert_titles(
        db_session, [make_record(2, "Portal", first_release_date=1191974400)]
    )

    assert (renamed.updated, renamed.near_duplicates) == (0, 1)
    assert [title.year for title in await db_session.scalars(select(Title).order_by(Title.id))] == [
        2007,
        2011,
    ]


async def stream(games: list[IgdbGame]) -> AsyncIterator[IgdbGame]:
    for game in games:
        yield game


# Each row: the titles already stored (IGDB id, name, year), the games of the run (IGDB id, name,
# year, ratings, franchise) and the stored titles after the run (IGDB id, name, franchise).
ORDER_CASES = {
    # A stored title is renamed while a new game takes its old name. The new one has the lower id
    # (it is handled first) or the higher one, and every batch holds one record.
    "rename_then_new_game_with_the_old_name": (
        [(5, "Foo", 2001)],
        [(3, "Foo", 2001, 5, None), (5, "Foo Remastered", 2001, 5, None)],
        {(3, "Foo", None), (5, "Foo Remastered", None)},
    ),
    "rename_then_new_game_with_a_higher_id": (
        [(5, "Foo", 2001)],
        [(5, "Foo Remastered", 2001, 5, None), (7, "Foo", 2001, 5, None)],
        {(5, "Foo Remastered", None), (7, "Foo", None)},
    ),
    # X takes Y's name and Y moves on, in one run.
    "chain_of_renames": (
        [(1, "A", 2001), (2, "B", 2001)],
        [(1, "B", 2001, 5, None), (2, "C", 2001, 5, None)],
        {(1, "B", None), (2, "C", None)},
    ),
    "swap_of_names": (
        [(1, "A", 2001), (2, "B", 2001)],
        [(1, "B", 2001, 5, None), (2, "A", 2001, 5, None)],
        {(1, "B", None), (2, "A", None)},
    ),
    # A rename blocked by a title outside the run stays put, and its old name stays taken.
    "blocked_rename_keeps_its_old_name_taken": (
        [(None, "C", 2001), (1, "A", 2001), (2, "B", 2001)],
        [(1, "C", 2001, 5, None), (2, "A", 2001, 5, None)],
        {(None, "C", None), (1, "A", None), (2, "B", None)},
    ),
    # The block goes down a long chain: each stored title wants the old name of the one before it,
    # and the first wants a name that a title outside the run holds.
    "blocked_rename_blocks_a_chain_four_deep": (
        [(None, "E", 2001), (1, "A", 2001), (2, "B", 2001), (3, "C", 2001), (4, "D", 2001)],
        [
            (1, "E", 2001, 5, None),
            (2, "A", 2001, 5, None),
            (3, "B", 2001, 5, None),
            (4, "C", 2001, 5, None),
        ],
        {(None, "E", None), (1, "A", None), (2, "B", None), (3, "C", None), (4, "D", None)},
    ),
    # No year matches no year (AC-14): an undated title outside the run holds the undated name.
    "undated_title_outside_holds_the_undated_name": (
        [(None, "C", None), (1, "A", None)],
        [(1, "C", None, 5, None), (2, "C", None, 900, None)],
        {(None, "C", None), (1, "A", None)},
    ),
    # A rename that a title outside the run blocks still writes the franchise.
    "blocked_rename_keeps_its_franchise": (
        [(None, "C", 2001), (1, "A", 2001)],
        [(1, "C", 2001, 5, "Series")],
        {(None, "C", None), (1, "A", "Series")},
    ),
    # A stored title that takes a name wins over a new game with more ratings (never replace a
    # stored title); among new games, more ratings win (AC-14).
    "stored_title_taking_the_name_wins": (
        [(1, "Bar", 2001)],
        [(1, "Foo", 2001, 5, None), (4, "Foo", 2001, 900, None)],
        {(1, "Foo", None)},
    ),
    "two_stored_titles_take_one_name": (
        [(1, "A", 2001), (2, "B", 2001)],
        [(1, "Foo", 2001, 5, None), (2, "Foo", 2001, 900, None)],
        {(1, "A", None), (2, "Foo", None)},
    ),
    "more_ratings_win_among_new_games": (
        [],
        [(3, "Foo", 2001, 10, None), (4, "Foo", 2001, 900, None)],
        {(4, "Foo", None)},
    ),
    # The stored title wins the name over a game with more ratings, so it keeps its updates.
    "stored_title_wins_the_name": (
        [(5, "Foo", 2001)],
        [(3, "Foo", 2001, 900, None), (5, "Foo", 2001, 10, "Series")],
        {(5, "Foo", "Series")},
    ),
}


@pytest.mark.parametrize(
    ("stored", "run", "expected"), list(ORDER_CASES.values()), ids=list(ORDER_CASES)
)
async def test__import__result_does_not_depend_on_the_order_and_a_second_run_changes_nothing(
    db_session: AsyncSession,
    stored: list[tuple[int | None, str, int | None]],
    run: list[tuple[int, str, int | None, int, str | None]],
    expected: set[tuple[int | None, str, str | None]],
) -> None:
    for igdb_id, name, year in stored:
        await create_title(db_session, name=name, year=year, igdb_id=igdb_id)
    # Mid-2001 and mid-2002 timestamps are far from a year edge.
    released = {2001: 995000000, 2002: 1030000000, None: None}

    async def run_once() -> titles_import.UpsertCounts:
        games = [
            make_game(
                id=igdb_id,
                name=name,
                first_release_date=released[year],
                total_rating_count=ratings,
                **({"franchise": {"id": 1, "name": franchise}} if franchise else {}),
            )
            for igdb_id, name, year, ratings, franchise in run
        ]
        selection = await select_records(stream(games))
        # One record to a batch: the keys cross every batch boundary.
        return await upsert_titles(db_session, selection.records, batch_size=1)

    async def stored_now() -> set[tuple[int | None, str, str | None]]:
        result = await db_session.execute(select(Title.igdb_id, Title.name, Title.franchise))
        return {(igdb_id, name, franchise) for igdb_id, name, franchise in result}

    first = await run_once()
    after_first = await stored_now()
    second = await run_once()

    assert after_first == expected
    # All updates in one transaction (a chain or a swap is never half committed), then one commit
    # for each added title, as a batch holds one.
    assert first.commits == (1 if first.updated else 0) + first.added
    assert (second.added, second.updated, second.refused) == (0, 0, 0)
    assert await stored_now() == after_first


async def test__import__a_run_of_many_batches_looks_up_plans_and_commits_in_every_batch(
    db_session: AsyncSession,
) -> None:
    for igdb_id, name in [(1, "One"), (2, "Two"), (3, "Three")]:
        await create_title(db_session, name=name, year=2001, igdb_id=igdb_id)
    await create_title(db_session, name="Held", year=2001)  # made by hand, no IGDB id
    answer = await create_title(db_session, name="Answer", year=2001)
    guessed = await db_session.scalar(select(Title).where(Title.igdb_id == 1))
    assert guessed is not None
    puzzle = await create_puzzle(
        db_session,
        game=await create_game(db_session),
        answer=answer,
        puzzle_date=dt.date(2026, 11, 1),
    )
    await play_stored_game(db_session, puzzle, guessed=[guessed])
    # Three stored titles and eight records, two to a batch: the lookup of the stored titles, of
    # those a stored game uses and of the holders of a key each take several batches, the title
    # a stored game uses is in the first one, and the hand-made title's key is in a late one.
    records = [
        make_record(igdb_id, name, first_release_date=995000000)  # mid-2001
        for igdb_id, name in [
            (1, "One Renamed"),  # refused
            (2, "Two Renamed"),
            (3, "Three Renamed"),
            (4, "Four"),
            (5, "Five"),
            (6, "Held"),  # a near-duplicate of the hand-made title
            (7, "Seven"),
            (8, "Eight"),
        ]
    ]

    first = await upsert_titles(db_session, records, batch_size=2)
    second = await upsert_titles(db_session, records, batch_size=2)

    result = await db_session.execute(select(Title.igdb_id, Title.name).where(Title.igdb_id > 0))
    assert sorted(result.tuples().all()) == [
        (1, "One"),
        (2, "Two Renamed"),
        (3, "Three Renamed"),
        (4, "Four"),
        (5, "Five"),
        (7, "Seven"),
        (8, "Eight"),
    ]
    assert await db_session.scalar(select(func.count()).select_from(Title)) == 9
    # One commit for the updates, then one for each batch of two added titles.
    assert (first.refused, first.updated, first.added, first.near_duplicates, first.commits) == (
        1,
        2,
        4,
        1,
        3,
    )
    assert (second.updated, second.added, second.refused, second.commits) == (0, 0, 1, 0)
