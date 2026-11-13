"""The title import and the target guard (M1-21, SPEC-04 AC-10 to AC-19, R-13, R-15).

No network: the IGDB client reads `tests/fixtures/igdb/games_import.json` over
`httpx.MockTransport`. The guard is checked with an environment mapping of the test's own."""

import datetime as dt
import json
from collections import Counter
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
    ],
    ids=[
        "main_franchise_first",
        "first_series_by_id",
        "neither",
        "qualifier_and_spaces_cleaned",
        "empty_after_cleaning_passes_to_next",
        "override_applied",
        "too_long_for_the_column",
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
        ("Zelda: Ocarina of Time", 912470400, ("zelda ocarina of time", 1998)),
        ("Hades", -631152000, ("hades", 1950)),
    ],
    ids=["longest_name", "name_too_long", "year_from_timestamp", "year_1950"],
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
    assert report.selection.near_duplicates == 1
    assert report.selection.out_of_scope == 3
    assert report.selection.name_problems == {NameProblem.TOO_LONG: 1}
    assert report.skipped == {SkipReason.NAME_WITHOUT_LETTERS: 1}
    assert report.dropped == {DropReason.GENRE_INVALID: 1}
    lines = "\n".join(report.lines())
    assert "records skipped as invalid (AC-8): 1" in lines
    assert "items dropped from records that were kept: 1" in lines


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
    user = User(email="player@example.com", hashed_password="not-a-real-hash")
    db_session.add(user)
    await db_session.flush()
    progress = Progress(
        user_id=user.id,
        puzzle_id=played.id,
        status="in_progress",
        started_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
        counts_for_streak=True,
        origin=ProgressOrigin.PLAY,
    )
    db_session.add(progress)
    await db_session.flush()
    db_session.add_all(
        [
            ProgressGuess(progress_id=progress.id, position=1, title_id=ids[1].id),
            ProgressGuess(progress_id=progress.id, position=2, title_id=ids[3].id),
        ]
    )
    await db_session.flush()

    second = [
        make_record(1, "Guessed Again", franchise={"id": 1, "name": "Old"}),  # renamed: refused
        make_record(2, "Answer", franchise={"id": 2, "name": "New"}),  # franchise: refused
        make_record(3, "DISPLAY"),  # same normalized name: allowed
        make_record(4, "Planned Two"),  # answer of a puzzle with no games: allowed
        make_record(5, "Free Two"),  # allowed
        # igdb 6 is missing from this run (AC-17).
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
    }
    assert ids[2].franchise == "Old"
    assert (counts.refused, counts.updated, counts.unchanged) == (2, 3, 0)
    assert sorted(counts.refused_title_ids) == sorted([ids[1].id, ids[2].id])


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
        db_session, [make_record(2, "portal", first_release_date=1191974400)]
    )

    assert (renamed.updated, renamed.near_duplicates) == (0, 1)
    assert [title.year for title in await db_session.scalars(select(Title).order_by(Title.id))] == [
        2007,
        2011,
    ]
