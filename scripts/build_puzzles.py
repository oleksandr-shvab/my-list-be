"""The puzzle builder's command line (SPEC-04, M1-22). Today it has one mode, `--counts`.

    uv run python scripts/build_puzzles.py --counts [--slug game-guesser]

`--counts` (AC-24) reads the candidate games from IGDB and prints how many pass each filter, in
order, so the owner can tune the thresholds (`CANDIDATE_*` in `.env`, AC-22) before building.
It writes nothing and prints no game name. It needs IGDB_CLIENT_ID and IGDB_CLIENT_SECRET in `.env`
(never commit or print them) and the app's own settings (`DATABASE_URL`, `SECRET_KEY`). The
database is read for the game's earlier answers and "never" exclusions; a game that is not in the
database yet has none. It takes about as long as the title import.

Exit code 0 on success, 1 when a setting is missing or IGDB or the database cannot be read.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import httpx
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.content.candidates import IGDB_CANDIDATES_WHERE, Funnel, funnel, never_igdb_ids
from app.content.fetch import FetchError
from app.content.igdb import IgdbClient, IgdbError
from app.content.settings import ContentSettings, MissingSettingError
from app.core.clock import get_now, utc_today

DEFAULT_SLUG = "game-guesser"


def counts_lines(result: Funnel) -> list[str]:
    """One "filter: passed N" line for each filter. No game name, no id."""
    return [f"{step.name}: passed {step.passed}" for step in result.steps]


async def run_counts(settings: ContentSettings, slug: str) -> Funnel:
    from app.core.db import async_session_maker, engine
    from app.crud.exclusion import list_exclusions
    from app.crud.game import get_game_by_slug
    from app.crud.puzzle import list_answer_igdb_ids

    try:
        now = get_now()
        async with async_session_maker() as db:
            game = await get_game_by_slug(db, slug)
            earlier = set() if game is None else await list_answer_igdb_ids(db, game.id)
            never = set() if game is None else never_igdb_ids(await list_exclusions(db, game.id))
        async with IgdbClient.from_settings(settings) as igdb:
            games = [game async for game in igdb.games(now=now, where=IGDB_CANDIDATES_WHERE)]
        return funnel(
            games,
            today=utc_today(now),
            settings=settings,
            earlier_answer_igdb_ids=earlier,
            never_igdb_ids=never,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--counts", action="store_true", help="print how many games pass each filter"
    )
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="the game whose answers are excluded")
    args = parser.parse_args()
    if not args.counts:
        parser.error("nothing to do: use --counts")
    # httpx logs every request URL at INFO; keep the run quiet.
    logging.basicConfig(level=logging.WARNING)
    settings = ContentSettings()
    try:
        settings.require("IGDB_CLIENT_ID", "IGDB_CLIENT_SECRET")
        result = asyncio.run(run_counts(settings, args.slug))
    except MissingSettingError as error:
        print(f"build_puzzles: refusing to run: {error}", file=sys.stderr)
        return 1
    except (IgdbError, FetchError, httpx.HTTPError) as error:
        print(f"build_puzzles: {error}", file=sys.stderr)
        return 1
    except (SQLAlchemyError, OSError) as error:
        print(f"build_puzzles: database error: {type(error).__name__}", file=sys.stderr)
        return 1
    print("\n".join(counts_lines(result)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
