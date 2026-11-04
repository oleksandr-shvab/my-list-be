"""Time the title search against 50,000 synthetic titles (AC-24). A manual check, not run in CI.

    ENVIRONMENT=local uv run python scripts/bench_title_search.py [--titles 50000]

It adds 50,000 made-up titles to the dev database, runs `VACUUM (ANALYZE)`, times about 200
searches (typos, words of a name, franchises, prefixes) and prints the median, the 95th percentile
and the slowest times in milliseconds. The target is a p95 under 150 ms. The titles use a small
vocabulary on purpose, so a typical search word matches about 2% of them: harder than real titles.

Every synthetic title has the year `BENCH_YEAR`, a name of the generated shape (`BENCH_NAME_PATTERN`)
and the video game kind, so the rows can always be found. A real title with the year 9999 as a
placeholder is not matched, because its name does not have that shape. The script deletes the rows
at the end (also when it fails) and at the start, in case an earlier run was killed: a `finally`
block does not run on SIGTERM, SIGHUP or `kill -9`. After each deletion it vacuums, so the dev
database is not left with the dead rows. The result is printed before the cleanup, and a cleanup
that fails is reported (exit code 1) without hiding the result.

Only the search is timed: the script holds one connection, warms it up and the query plan first,
and times `search_titles` per search. A timed search includes the lazy BEGIN and the
`set_config` of the similarity threshold, which every real request pays too. It does not include
checking a connection out of the pool or a pre-ping.

It refuses to run unless the ENVIRONMENT variable is exactly `local` and DATABASE_URL points at
this machine, the same rule as `seed_dev.py`.
"""

import argparse
import asyncio
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from seed_dev import refusal_reason
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.crud.title import create_title, search_titles
from app.games.normalize import normalize_text
from app.games.service import search_query
from app.models.title import Title, TitleKind

TITLE_COUNT = 50_000
# The marker of a synthetic title: no real title has this year, and the search ignores the year.
# The name pattern is the second half of the marker; `make_titles` checks every name against it.
BENCH_YEAR = 9999
_WORD = "[A-Z][a-z]+"
BENCH_NAME_PATTERN = rf"^(The )?{_WORD} {_WORD}(: {_WORD} {_WORD}| of the {_WORD} {_WORD})? [0-9]+$"
SEARCH_COUNT = 200
COMMIT_EVERY = 5_000
SEED = 24

ADJECTIVES = (
    "crimson",
    "silent",
    "broken",
    "golden",
    "lost",
    "hidden",
    "eternal",
    "frozen",
    "savage",
    "cosmic",
    "neon",
    "hollow",
    "ancient",
    "iron",
    "shattered",
    "wild",
    "royal",
    "dark",
    "radiant",
    "forgotten",
    "burning",
    "pale",
    "rusted",
    "electric",
    "lunar",
)
NOUNS = (
    "blade",
    "legends",
    "kingdom",
    "empire",
    "frontier",
    "quest",
    "odyssey",
    "chronicle",
    "warrior",
    "horizon",
    "outpost",
    "tide",
    "citadel",
    "harbor",
    "garden",
    "machine",
    "saga",
    "rift",
    "voyage",
    "vault",
    "arena",
    "engine",
    "dynasty",
    "relic",
    "uprising",
)
FRANCHISES = (
    "Starforge",
    "Ironvale",
    "Moonreach",
    "Duskwood",
    "Emberfall",
    "Stormgate",
    "Glasshaven",
    "Nightbloom",
    "Ashmoor",
)


def make_titles(rng: random.Random, count: int) -> list[tuple[str, str | None, int]]:
    """Made-up names in four shapes, such as "The Hollow Harbor", "Crimson Blade 3", "Neon Relic:
    Lost Garden" and "Iron Saga of the Pale Vault". Few names contain "the" or "of", as in a real
    catalogue. The number at the end keeps each name unique."""
    titles = []
    for number in range(count):
        adjective = rng.choice(ADJECTIVES).title()
        noun = rng.choice(NOUNS).title()
        shape = rng.random()
        if shape < 0.4:
            name = f"{adjective} {noun} {number}"
        elif shape < 0.6:
            name = f"The {adjective} {noun} {number}"
        elif shape < 0.8:
            name = f"{adjective} {noun}: {rng.choice(ADJECTIVES).title()} {rng.choice(NOUNS).title()} {number}"
        else:
            name = (
                f"{adjective} {noun} of the {rng.choice(ADJECTIVES).title()} "
                f"{rng.choice(NOUNS).title()} {number}"
            )
        franchise = rng.choice(FRANCHISES) if rng.random() < 0.4 else None
        if not re.fullmatch(BENCH_NAME_PATTERN, name):
            raise AssertionError(f"Generated name {name!r} does not match BENCH_NAME_PATTERN")
        titles.append((name, franchise, BENCH_YEAR))
    return titles


def make_queries(rng: random.Random, titles: list[tuple[str, str | None, int]]) -> list[str]:
    queries = []
    for _ in range(SEARCH_COUNT):
        name, franchise, _year = rng.choice(titles)
        words = normalize_text(name).split()
        kind = rng.choice(["typo", "word", "prefix", "two words", "franchise"])
        if kind == "typo":
            word = rng.choice(words[:4])
            cut = rng.randrange(len(word))
            queries.append(word[:cut] + word[cut + 1 :])
        elif kind == "word":
            queries.append(rng.choice(words[:4]))
        elif kind == "prefix":
            queries.append(normalize_text(name)[: rng.randint(3, 12)])
        elif kind == "two words":
            queries.append(" ".join(words[:2]))
        else:
            queries.append(franchise or words[0])
    return queries


async def run(count: int) -> bool:
    """Insert, time, clean up. False when the cleanup failed (the result was printed already)."""
    from app.core.db import async_session_maker, engine

    rng = random.Random(SEED)
    titles = make_titles(rng, count)
    queries = make_queries(rng, titles)
    is_clean = True
    try:
        await remove_bench_titles(engine, vacuum_when_empty=False)
        async with async_session_maker() as db:
            for start in range(0, count, COMMIT_EVERY):
                for name, franchise, year in titles[start : start + COMMIT_EVERY]:
                    await create_title(db, name=name, franchise=franchise, year=year)
                await db.commit()
        # What autovacuum does after a bulk load: it refreshes the statistics and empties the GIN
        # indexes' pending lists (every search reads the whole list until then).
        await vacuum(engine)
        print(f"Inserted {count} titles.")
        await time_searches(engine, queries)
    finally:
        # A failed cleanup is reported, never raised: it must not hide the result above or the
        # error that ended the run.
        try:
            await remove_bench_titles(engine, vacuum_when_empty=True)
        except (SQLAlchemyError, OSError) as error:
            is_clean = False
            print(
                f"CLEANUP FAILED ({error!r}). Delete the rows by hand: DELETE FROM titles "
                f"WHERE year = {BENCH_YEAR} AND name ~ '{BENCH_NAME_PATTERN}'.",
                file=sys.stderr,
            )
        await engine.dispose()
    return is_clean


async def time_searches(engine: AsyncEngine, queries: list[str]) -> None:
    timed: list[tuple[float, str, int]] = []
    async with engine.connect() as connection:
        # Warm-up, not timed: the connection, the prepared statements and the plan.
        async with AsyncSession(bind=connection) as warm_up:
            await warm_up.execute(select(1))
            for query in queries:
                if (normalized := search_query(query)) is not None:
                    await search_titles(
                        warm_up, kind=TitleKind.VIDEO_GAME, normalized_query=normalized
                    )
                    break
        for query in queries:
            # The same rule as the service: a short or empty query never reaches the database.
            if (normalized := search_query(query)) is None:
                continue
            # A fresh session on the held connection is one transaction per search, as in a
            # request, so the transaction-local threshold is set again every time.
            async with AsyncSession(bind=connection) as db:
                started = time.perf_counter()
                found = await search_titles(
                    db, kind=TitleKind.VIDEO_GAME, normalized_query=normalized
                )
                elapsed_ms = (time.perf_counter() - started) * 1000
            timed.append((elapsed_ms, normalized, len(found)))
    for slow_ms, slow_query, _size in sorted(timed, reverse=True)[:5]:
        print(f"  slow: {slow_ms:.1f} ms for {slow_query!r}")
    timings_ms = sorted(elapsed for elapsed, _query, _size in timed)
    p95 = timings_ms[math.ceil(0.95 * len(timings_ms)) - 1]
    print(
        f"{len(timings_ms)} searches: median {statistics.median(timings_ms):.1f} ms, "
        f"p95 {p95:.1f} ms, max {timings_ms[-1]:.1f} ms, "
        f"median result size {statistics.median(size for _e, _q, size in timed)}."
    )
    print(f"AC-24 target: p95 under 150 ms. {'PASS' if p95 < 150 else 'FAIL'}.")


async def remove_bench_titles(engine: AsyncEngine, *, vacuum_when_empty: bool) -> None:
    """Delete every synthetic title (also leftovers of a killed run), then vacuum. A title is
    synthetic when its year, name shape and kind all match."""
    async with AsyncSession(engine) as db:
        result = await db.execute(
            delete(Title).where(
                Title.year == BENCH_YEAR,
                Title.kind == TitleKind.VIDEO_GAME,
                Title.name.op("~")(BENCH_NAME_PATTERN),
            )
        )
        await db.commit()
    removed = result.rowcount
    if removed:
        print(f"Removed {removed} synthetic titles.")
    if removed or vacuum_when_empty:
        await vacuum(engine)


async def vacuum(engine: AsyncEngine) -> None:
    """VACUUM cannot run inside a transaction."""
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("VACUUM (ANALYZE) titles"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Time the title search (AC-24).")
    parser.add_argument("--titles", type=int, default=TITLE_COUNT, help="rows to insert")
    args = parser.parse_args()
    reason = refusal_reason()
    if reason is not None:
        print(f"Refusing to run the benchmark: {reason}.")
        return 1
    return 0 if asyncio.run(run(args.titles)) else 1


if __name__ == "__main__":
    sys.exit(main())
